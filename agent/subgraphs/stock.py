r"""炒股 agent 子图（有界 ReAct 循环，进程内工具直连取数）。

流程:
    tool_call --(有 tool_calls 且未超轮次)--> tool_execute --┐
       ↑                                                    │
       └────────────────────────────────────────────────────┘
    tool_call --(无 tool_calls / 已达轮次上限)--> synthesize --> END

tool_execute 完回到 tool_call，让模型看到结果再决定「继续调」还是「收尾作答」，
以完成「先 symbol_search 拿代码、再用代码 get_quote/get_valuation」的多跳链路；
轮次由 _MAX_TOOL_ROUNDS 封顶，避免无限循环。

- tool_call: LLM 决定是否/如何调工具（streaming=False，不外泄推理 token）。
- tool_execute: 并发执行本轮所有 tool_calls（工具内置超时 + 重试 + 降级），
  每个调用前后发 tool_start / tool_end 事件；tool_results 跨轮累加，synthesize 能看到全部数据。
- synthesize: 用 prompts/stock.xml + 累计 tool_results 流式生成回答，
  并把行情/K线/估值等结构化数据以 card/table 事件独立发射。

工具源：12 个取数 @tool 全部进程内构造，由 LLM 的 tool_calls 直接驱动（无子进程、无协议层）；
另挂 search_knowledge_base 供模型补充研报/概念检索。
卡片发射前过 pydantic 校验（subgraphs/cards.py），失败降级为纯文本。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from config import MAX_INPUT_TOKENS, TOOL_CONCURRENCY
from core.llm import estimate_tokens, get_llm, is_context_overflow, message_chars
from core.state import AgentState, recent_turns
from core.template import load_template, render
from subgraphs import emit, emit_card, emit_table
from subgraphs.cards import validate_card, validate_table
from tools import get_tools, tool_cache_get, tool_cache_put
from tools.rag_tool import search_knowledge_base

logger = logging.getLogger("stocka.agent")

# 工具调用系统提示在 prompts/stock_toolcall.xml，由 render() 注入 tool_call 节点，不在代码里硬编码

# 多跳轮次上限：tool_execute 每轮回到 tool_call，超过即强制 synthesize，防无限循环；
# 3 轮足够覆盖「搜代码 -> 查行情 -> 查估值/资金流」这类链式取数
_MAX_TOOL_ROUNDS = 3

# 单条消息允许进入 tool_call 上下文的最大字符数（见 _current_run_messages）
_MAX_MSG_CHARS = 2000
# synthesize 里塞进提示词的工具结果上限（字符）
_MAX_TOOL_CHARS = 6000

# 工具名 -> 中文标签（tool_start 事件用，前端展示「正在获取 xx」）；未登记的退回原始工具名
_TOOL_LABELS = {
    "get_quote": "实时行情",
    "symbol_search": "股票搜索",
    "get_industry_peers": "同行业公司",
    "get_valuation": "估值指标",
    "get_financial_indicator": "财务指标",
    "get_financial_report": "财报数据",
    "get_money_flow": "资金流向",
    "get_stock_news": "财经资讯",
    "get_history_daily": "历史行情",
    "get_top_list": "龙虎榜席位",
    "get_peer_compare": "同业对比",
    "get_research_reports": "机构研报",
    "search_knowledge_base": "知识库检索",
}

# 事件类型：从资讯标题归纳（数据源只有标题/内容/来源，没有事件分类字段）。
# 顺序敏感：越具体的关键词排越前（「减持」与「增持」互不重叠，「诉讼」在「处罚」之前）
_EVENT_TYPE_RULES = (
    ("解禁", "解禁"),
    ("减持", "增减持"),
    ("增持", "增减持"),
    ("回购", "回购"),
    ("停牌", "停复牌"),
    ("复牌", "停复牌"),
    ("分红", "分红"),
    ("派息", "分红"),
    ("诉讼", "诉讼"),
    ("仲裁", "诉讼"),
    ("违规", "诉讼"),
    ("处罚", "诉讼"),
    ("中标", "订单"),
    ("签约", "订单"),
    ("订单", "订单"),
    ("业绩", "财报"),
    ("预告", "财报"),
    ("年报", "财报"),
    ("中报", "财报"),
    ("季报", "财报"),
)

# 需要用户在时间轴上重点关注的事件类型（前端圆点标金）
_EVENT_IMPORTANT_TYPES = ("解禁", "停复牌", "诉讼")

# 事件卡摘要的最大字数：资讯正文可能很长，卡内只放一句
_EVENT_DESC_CHARS = 60

# 工具集进程级缓存：首轮 tool_call 前经 _ensure_tools 懒加载
_TOOLS = None


async def _ensure_tools() -> None:
    """懒加载工具集：进程内 @tool 直连 + 知识库检索工具。

    全部是 LangChain 工具对象，由 LLM 的 tool_calls 直接驱动，不经过子进程或协议层。
    """
    global _TOOLS
    if _TOOLS is not None:
        return
    tools = list(get_tools())
    try:
        tools.append(search_knowledge_base)
    except Exception:  # noqa: BLE001 - 检索工具构造失败不阻断取数工具
        pass
    _TOOLS = tools


async def warm_tools() -> int:
    """启动时预热工具集，返回工具数量；失败返回 0 由首请求重试。"""
    try:
        await _ensure_tools()
        return len(_tools())
    except Exception:  # noqa: BLE001 - 预热失败不影响启动
        return 0


def _tools() -> list:
    """同步取当前工具集；_ensure_tools 尚未跑过时退回直连工具。"""
    if _TOOLS is None:
        return get_tools()
    return _TOOLS


def _tools_by_name():
    return {t.name: t for t in _tools()}


# 绑定工具后的模型（进程级缓存）：bind_tools 每次都要把 13 个工具的 schema 转一遍，
# 而一轮对话里 tool_call 最多跑 3 次，重复构造纯属浪费
_bound_llm = None


def _tool_bound_llm():
    global _bound_llm
    if _bound_llm is None:
        _bound_llm = get_llm(streaming=False, temperature=0.2).bind_tools(_tools())
    return _bound_llm


def _recent_history(state: dict, limit: int = 6) -> list:
    """最近 limit 条纯文本历史（复用 state.recent_turns 的过滤口径），并逐条截断正文。

    工具消息与残缺 tool_call 已由 recent_turns 统一剔除，这里只保留 _shrink 的截断职责：
    只截正文、不重建消息类型，避免破坏 tool_call_id 配对让上游接口报错。
    """
    return [_shrink(m) for m in recent_turns(state, limit, query=state.get("query"))]


def _shrink(msg):
    """把单条消息的正文截到 _MAX_MSG_CHARS，字段（tool_call_id 等）原样保留。

    只截正文、不重建消息类型：工具调用协议依赖 tool_call_id 与消息类型的配对，
    重建不当会产出孤儿 ToolMessage / 残缺 tool_call，直接让 LLM 接口报错。
    """
    content = getattr(msg, "content", "")
    if isinstance(content, str) and len(content) > _MAX_MSG_CHARS:
        try:
            return msg.model_copy(update={"content": content[:_MAX_MSG_CHARS] + "\n…[已截断]"})
        except Exception:  # noqa: BLE001 - 拷贝失败就退回原消息，宁可长也不能破坏协议
            return msg
    return msg


def _current_run_messages(state: dict) -> list:
    """取「本轮」的消息：从最后一条 HumanMessage 起直到末尾。

    tool_call 必须看到本轮已取到的工具结果，才能决定「继续取数」还是「收尾作答」
    （ReAct 多跳）。但绝不能带上之前轮次的工具结果：实测第二轮会因为「历史里
    已经有 K 线数据」而不再发起调用，直接凭印象作答（答成「无法获取」）。
    只取本轮顺带把上下文锁在一轮之内，不会随会话轮数累积到几十万 token。

    指代消解贯通：rewrite 节点已把「它的市盈率是多少」改写成「贵州茅台的市盈率
    是多少」，但 tool_call 只喂本轮消息时模型看到的是没有主语的「它」——
    实测多轮第二轮因此一个工具都不调，3 轮重试后落入无数据兜底（MT-01）。
    这里把本轮首条 HumanMessage 的正文替换为 rewritten（仅影响本次调用，
    model_copy 不改状态里的原消息；tool_call_id 配对不涉及 human 消息）。
    """
    msgs = list(state.get("messages", []))
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], HumanMessage):
            head = msgs[i:]
            return _apply_rewrite(head, state)
    return msgs


def _apply_rewrite(run_msgs: list, state: dict) -> list:
    """把本轮首条用户消息的正文替换为指代消解后的 rewritten（有值时）。"""
    rewritten = (state.get("rewritten") or "").strip()
    query = (state.get("query") or "").strip()
    if not rewritten or rewritten == query or not run_msgs:
        return run_msgs
    first = run_msgs[0]
    content = first.content if isinstance(first.content, str) else ""
    if content.strip() != query:
        return run_msgs
    try:
        return [first.model_copy(update={"content": rewritten})] + run_msgs[1:]
    except Exception:  # noqa: BLE001 - 拷贝失败就退回原消息，不让链路断
        return run_msgs


# 取数类工具：只有它们的结果才能支撑回答里的数字。
# K 线（get_history_daily）、资讯（get_stock_news）、财务指标（get_financial_indicator）、
# 龙虎榜（get_top_list）、同业对比（get_peer_compare）同属此类——
# 不进白名单的话，用户问「最近日K/新闻/ROE」即使取数成功，
# 也会被 _no_data_fallback 的兜底文案整段覆盖（实测必踩）
_DATA_TOOLS = (
    "get_quote", "get_valuation", "get_financial_report", "get_money_flow",
    "get_history_daily", "get_stock_news", "get_financial_indicator",
    "get_top_list", "get_peer_compare",
)


def _got_data(state: dict) -> bool:
    """本轮是否已经取到取数类工具的数据。"""
    for item in (state.get("tool_results") or []):
        if item.get("tool") in _DATA_TOOLS and (item.get("result") or {}).get("ok"):
            return True
    return False


def _needs_data_fetch(state: dict) -> bool:
    """这一轮是否必须去取真实数据（用户要数据 + 不是概念问题）。

    概念问题（如「解释一下 PE 和 PB 的区别」）不需要真实数据，不要逼它去取数。
    """
    if _is_concept_question(state):
        return False
    return _asked_for_data(state)


def _pending_nudge(state: dict) -> str:
    """还没取到任何行情数据、且用户要的是数据时，追加一条强制取数的指令。

    「一轮只调了 symbol_search 就作答」是实测中最常见的失效姿势：模型靠历史消息
    里的旧数字拼出一份看似完整的答案，实际是过期数据；「一个工具都不调直接作答」
    同理（实测偶发：问行情却直接答「没取到数据」）。

    两个讲究：
    1. 只在第二轮起追加。第一轮系统提示已经写了「必须调用工具」，再补一遍
       等于给模型两条同义指令，实测会让它把提示里的词直接抄进函数名槽位
       （输出成 `function调用`），反而调不出工具；
    2. 措辞里避开「调用」二字，原因同上（模型会把它当成函数名）。
    """
    if _call_rounds(state) < 1:
        # 第一轮：不加提示，交给系统提示约束
        return ""
    results = state.get("tool_results") or []
    codes = []
    for item in results:
        name = item.get("tool")
        res = item.get("result") or {}
        if not res.get("ok"):
            continue
        if name == "symbol_search":
            for row in (res.get("data") or [])[:3]:
                if isinstance(row, dict) and row.get("code"):
                    codes.append(str(row["market"] or "") + str(row["code"]))
    if _got_data(state) or not _needs_data_fetch(state):
        return ""
    hint = "必须用工具取真实数据（行情用 get_quote，参数见工具列表）"
    if codes:
        hint = "已检索到代码：" + "、".join(codes) + "。" + hint
    return (
        hint + "；禁止直接作答，也禁止引用历史消息里的数字（那是上一轮的过期数据）。"
    )


# 有些模型（实测 DeepSeek-V3 经 SiliconFlow）不填 tool_calls，而把函数调用写进正文：
#     function
#     get_quote
#     ```json
#     {"code":"SH600519"}
#     ```
# 这种「文本形态的工具调用」在图里等于没调工具：取数不会发生、回答退化成兜底文案，
# 这里把它还原成真正的 tool_call。
_TEXT_TOOL_RE = re.compile(r"function\s*\n?\s*([A-Za-z_][A-Za-z0-9_]*)")
_TEXT_TOOL_RE2 = re.compile(r"(?:^|\n)\s*([A-Za-z_][A-Za-z0-9_]*)\s*\n\s*\{")
# JSON 前缀里最后一个标识符（用来找工具名）
_TRAILING_IDENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _extract_json_object(text: str, start: int) -> str:
    """从 text 的 start 之后找出第一个配平的 JSON 对象（跳过字符串里的括号）。"""
    i = text.find("{", start)
    if i < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def _parse_text_tool_call(content: str):
    """从正文里解析出 (工具名, 参数)；解析不出返回 (None, None)。

    两种来源都认：
    1. 正文中写了工具名（如 `function\\nget_quote\\n{...}`）；
    2. 工具名缺失或被写坏（实测会写成 `function调用`），此时按参数签名反推
       （只带 code -> 行情，带 query -> 搜索）。
    """
    if not content or "{" not in content:
        return None, None
    known = _tools_by_name()
    for raw, prefix in _iter_json_objects(content):
        try:
            args = json.loads(raw)
        except Exception:  # noqa: BLE001 - 解析失败就当没这回事
            continue
        if not isinstance(args, dict):
            continue
        name = _tool_name_in(prefix)
        if name in known:
            return name, args
        inferred = _infer_tool_by_args(args)
        if inferred:
            return inferred, args
    return None, None


def _iter_json_objects(content: str):
    """逐个产出内容里的 JSON 对象及其前面的文本。"""
    i = 0
    n = len(content)
    while i < n:
        if content[i] != "{":
            i += 1
            continue
        raw = _extract_json_object(content, i)
        if not raw:
            i += 1
            continue
        yield raw, content[:i]
        i += len(raw)


def _tool_name_in(prefix: str):
    """从 JSON 前面的文本里找工具名（找不到返回 None）。"""
    # 中间可能隔着 ```json 围栏，先去掉再找最后一个标识符
    stripped = re.sub(r"```(?:json)?", " ", prefix)
    m = _TRAILING_IDENT_RE.search(stripped)
    if m and m.group(1) in _tools_by_name():
        return m.group(1)
    return None


def _infer_tool_by_args(args: dict):
    """按参数签名反推工具名（拿不准就不猜，返回 None）。"""
    if "query" in args:
        return "symbol_search"
    if "peers" in args:
        return "get_peer_compare"
    # top_n 是 get_industry_peers 独有参数：code+limit 会被判成资讯，所以它不用 limit 命名
    if "top_n" in args:
        return "get_industry_peers"
    if "start_time" in args or "end_time" in args:
        return "get_stock_news"
    if "code" in args:
        rest = {k for k in args if k != "code"}
        if not rest:
            return "get_quote"
        if rest <= {"days", "adj", "end_date"}:
            return "get_history_daily"
        if "limit" in rest:
            return "get_stock_news"
    return None


def _normalize_tool_call(ai: AIMessage) -> AIMessage:
    """把「文本形态的工具调用」还原成结构化 tool_call。

    还原后原正文会被清空：正文里那段 function 文本对模型没有意义，
    留着反而可能让它继续用文本形态回答。
    """
    if getattr(ai, "tool_calls", None):
        return ai
    content = ai.content if isinstance(ai.content, str) else ""
    name, args = _parse_text_tool_call(content)
    if not name:
        return ai
    call_id = f"call_{name}_{uuid.uuid4().hex[:8]}"
    logger.info("restore text-form tool call: %s", name)
    return ai.model_copy(update={
        "content": "",
        "tool_calls": [{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    })


async def tool_call(state: dict) -> dict:
    # 首轮先懒加载工具集（进程内 @tool，见 _ensure_tools）
    await _ensure_tools()
    # 只喂本轮消息（含本轮已取到的工具结果），逐条截断正文，防止上下文膨胀
    run_msgs = [_shrink(m) for m in _current_run_messages(state)]
    if not run_msgs:
        # 兜底：拿不到本轮消息时至少把问题本身交给模型
        run_msgs = [HumanMessage(content=state.get("query", "") or "")]
    msgs = [SystemMessage(content=render(
        "stock_toolcall.xml", examples=load_template("toolcall_examples.xml")))] + run_msgs
    nudge = _pending_nudge(state)
    if nudge:
        msgs.append(SystemMessage(content=nudge))
    llm = _tool_bound_llm()
    try:
        ai = await llm.ainvoke(msgs)
    except Exception as e:  # noqa: BLE001
        if not is_context_overflow(e):
            raise
        # 上下文超长：这一轮不再调工具，直接进 synthesize（那里有降级文案），
        # 绝不让超长请求冒泡出去把整条对话变成空
        logger.warning("tool_call context overflow, skip tool round", exc_info=True)
        return {"messages": [AIMessage(content="")]}
    # 模型偶尔把工具调用写成正文而不是结构化字段，这里还原成真正的 tool_call
    return {"messages": [_normalize_tool_call(ai)]}


def _call_rounds(state: dict) -> int:
    """tool_call 节点已执行的轮数 = 本轮消息里 AIMessage 的条数。

    比「带 tool_calls 的 AIMessage 数」更准，两个原因：
    1. 模型偶尔一轮不调工具直接作答，那个 AIMessage 也要计入，
       否则「没调工具 -> 再来一轮」的重试会变成死循环；
    2. 只数本轮（从最后一个 HumanMessage 起），不把历史轮次算进来——
       否则多轮对话里轮次上限会被历史提前吃光。
    """
    return sum(1 for m in _current_run_messages(state) if isinstance(m, AIMessage))


def route_after_call(state: dict) -> Literal["tool_execute", "tool_call", "synthesize"]:
    """路由：该取数就去取，该取而没取就再给一次机会，最后才合成。

    - 有 tool_calls 且未超轮次上限 -> 执行工具
    - 模型一轮都没调工具，但用户要的是数据 -> 再回 tool_call 一次
      （实测偶发「问行情却直接答没取到数据」，靠重试逼它去取真实数据）
    - 其余（已达上限 / 概念问题 / 不需要数据）-> 合成
    """
    msgs = state.get("messages", [])
    last = msgs[-1] if msgs and isinstance(msgs[-1], AIMessage) else None
    if last is not None and getattr(last, "tool_calls", None):
        return "tool_execute" if _call_rounds(state) <= _MAX_TOOL_ROUNDS else "synthesize"
    if (
        _call_rounds(state) < _MAX_TOOL_ROUNDS
        and _needs_data_fetch(state)
        and not _got_data(state)
    ):
        return "tool_call"
    return "synthesize"


async def tool_execute(state: dict) -> dict:
    """并发执行 last AIMessage 的所有 tool_calls，收集结构化结果。"""
    msgs = state.get("messages", [])
    ai = msgs[-1] if msgs and isinstance(msgs[-1], AIMessage) else None
    if ai is None or not ai.tool_calls:
        return {}

    by_name = _tools_by_name()
    # 并发闸门：模型一轮可能给出多个 tool_calls，akshare 走线程池，
    # 全放开会让多个 DataFrame 同时驻留（3GB 机器上没必要为省几十毫秒冒这个险）
    sem = asyncio.Semaphore(max(1, TOOL_CONCURRENCY))

    async def _run(tc):
        tool = by_name.get(tc["name"])
        args = tc.get("args", {}) or {}
        name = tc["name"]
        label = _TOOL_LABELS.get(name, name)
        if tool is None:
            return tc["id"], name, {"ok": False, "error": f"unknown tool {name}"}
        # 调用前：通知前端开始取数（emit 为同步函数，内部拿不到 writer 时静默降级）
        emit("tool_start", {"tool": name, "label": f"正在获取{label}"})

        cached = tool_cache_get(name, args)
        if cached is not None:
            # 命中缓存也要补一条 tool_end：前端靠它把 tool_start 插的占位卡收尾
            emit("tool_end", {"tool": name, "ok": bool(cached.get("ok")), "ms": 0})
            return tc["id"], name, cached

        t0 = time.monotonic()
        async with sem:
            try:
                result = await tool.ainvoke(args)
            except Exception as e:  # noqa: BLE001 - 工具本身已降级，这里再兜一层
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        # 调用后：回传成功与否及耗时
        emit("tool_end", {"tool": name, "ok": bool(result.get("ok")), "ms": elapsed_ms})
        tool_cache_put(name, args, result)
        return tc["id"], name, result

    outcomes = await asyncio.gather(*[_run(tc) for tc in ai.tool_calls])

    tool_messages = []
    tool_results = []
    for tc_id, name, result in outcomes:
        tool_messages.append(ToolMessage(
            content=json.dumps(result, ensure_ascii=False, default=str),
            tool_call_id=tc_id,
            name=name,
        ))
        tool_results.append({"tool": name, "result": result})

    # tool_results 已配置 operator.add reducer：这里只返回本轮新增结果，
    # 由 reducer 跨轮拼接到已有列表，synthesize 仍能看到全部轮次的工具数据
    return {"messages": tool_messages, "tool_results": tool_results}


def _emit_validated_card(card_type: str, payload) -> None:
    """带 schema 校验的卡片发射（双模式输出的结构化闸门）。

    载荷先过 subgraphs.cards 的 pydantic 校验：通过才发 card 事件；
    失败只丢弃这张卡并记日志——正文回答仍由 synthesize 的文本兜底，
    保证用户始终有答案（校验失败降级为纯文本，LangGraph 架构 §4.5）。
    """
    if not validate_card(card_type, payload):
        logger.warning("card %s payload failed validation, skip emit: %s",
                       card_type, str(payload)[:200])
        return
    emit_card(card_type, payload)


def _emit_validated_table(table_type: str, payload) -> None:
    """带 schema 校验的表格发射；校验失败只丢表，正文照常。"""
    if not validate_table(table_type, payload):
        logger.warning("table %s payload failed validation, skip emit: %s",
                       table_type, str(payload)[:200])
        return
    emit_table(table_type, payload)


def _emit_structured(tool_results: list) -> None:
    """把行情/估值/资金流/财报等结构化数据以独立 card/table 事件发射
    （不进入 token 流）。

    事件契约（与小程序 chat.js 的卡片工厂一一对应，改这里必须同步那边
    与 server/README.md 第三节）。八张金融卡全部由此下发：
    - get_quote            -> card: quote_card（行情速览卡）
    - symbol_search        -> table: symbol_list（股票搜索候选）
    - get_valuation        -> card: valuation_card（估值卡，最新交易日一行）
    - get_money_flow       -> card: fundflow_card（资金流向卡，最新交易日一行，
                              命中 get_top_list 时并入 lhb 席位区块）
    - get_financial_report -> card: report_card（财报速览卡，三表合并行）
    - get_history_daily    -> table: kline_table（K 线卡，最近 15 个交易日）
    - get_peer_compare     -> card: compare_card（同业对比卡，当日快照）
    - get_research_reports -> card: research_card（研报观点卡，最新一篇）
    - get_stock_news       -> card: event_card（公司事件卡，当日资讯折成时间轴）

    一次提问，一种卡片只发一张（防刷屏，前端每张卡是一条独立消息）：
    - 数据源按「天」返回的（估值 / 资金流）只取最新一天——按天发会把同一只
      股票的连续交易日并排铺三张（实测问一句估值出三张卡）；
    - 数据源按「条」返回的（研报 / 资讯）取最新一篇、或整轮合成一张时间轴，
      多条目之间的差异交给正文转述；
    - 行情 / 财报 / 对比 / K 线本来就是单对象或整表，天然一张。
    """
    lhb = _lhb_payload(tool_results)
    quote = _quote_payload(tool_results)
    has_moneyflow = False
    for item in tool_results:
        name = item.get("tool")
        result = item.get("result") or {}
        if not result.get("ok"):
            continue
        data = result.get("data")
        if name == "get_quote" and data:
            _emit_validated_card("quote_card", data)
        elif name == "symbol_search" and data:
            _emit_validated_table("symbol_list", data)
        elif name == "get_valuation" and data:
            # 估值卡是「当日估值快照」：工具按天返回一整段（默认 30 个交易日），
            # 只取最新一天。按天发多张会把同只股票的连续交易日并排刷屏
            # （实测问一句估值出三张卡），且与 compare_card 的当日快照口径不一致
            rows = _rows_of(data)
            if rows:
                _emit_validated_card("valuation_card", rows[0])
        elif name == "get_money_flow" and data:
            # 资金流卡同为当日快照，同上只取最新一天；
            # 一段历史资金流由正文回答，卡片不重复铺三张
            rows = _rows_of(data)
            has_moneyflow = bool(rows)
            if rows:
                # 龙虎榜并入这一张卡：同一标的的资金流 + 上榜席位一屏看完
                extra = {"lhb": lhb["rows"], "lhb_date": lhb["trade_date"]} if lhb else {}
                _emit_validated_card("fundflow_card", _fundflow_card_payload(rows[0], extra))
        elif name == "get_history_daily" and data:
            rows = _kline_rows(data)
            if rows:
                payload = {"rows": rows}
                # 表事件只带行，卡片标题缺标的名：本轮查过行情时补上代码与名称
                if quote:
                    payload["code"] = quote.get("code")
                    payload["name"] = quote.get("name")
                _emit_validated_table("kline_table", payload)
        elif name == "get_financial_report" and data:
            _emit_validated_card("report_card", data)
        elif name == "get_peer_compare" and data:
            _emit_validated_card("compare_card", data)
        elif name == "get_research_reports" and data:
            # 研报卡取最新一篇：多家机构的观点差异由正文转述，
            # 卡片只承担「最新机构观点速览」，一次提问一张
            rows = _rows_of(data.get("rows") if isinstance(data, dict) else data)
            if rows:
                _emit_validated_card("research_card", rows[0])
        elif name == "get_stock_news" and data:
            # 个股资讯整轮合成一张事件卡：时间轴本身就是多条，不需要一张一条
            payload = _event_card_payload(
                _rows_of(data.get("rows") if isinstance(data, dict) else data)
            )
            if payload:
                _emit_validated_card("event_card", payload)
    # 龙虎榜单独命中、本轮没有资金流数据时：发一张仅龙虎榜的资金流向卡
    if lhb and not has_moneyflow:
        _emit_validated_card(
            "fundflow_card", {"lhb": lhb["rows"], "lhb_date": lhb["trade_date"]}
        )


def _lhb_payload(tool_results: list):
    """取第一个成功的 get_top_list 结果作为龙虎榜区块载荷；没有返回 None。"""
    for item in tool_results:
        if item.get("tool") != "get_top_list":
            continue
        result = item.get("result") or {}
        if result.get("ok") and isinstance(result.get("data"), dict) and result["data"].get("rows"):
            return result["data"]
    return None


def _quote_payload(tool_results: list):
    """取第一个成功的 get_quote 结果（K 线卡的标的名从这里借）；没有返回 None。"""
    for item in tool_results:
        if item.get("tool") != "get_quote":
            continue
        result = item.get("result") or {}
        if result.get("ok") and isinstance(result.get("data"), dict) and result["data"]:
            return result["data"]
    return None


def _event_type(title: str) -> str:
    """资讯标题 -> 事件类型；归纳不出时归到「资讯」（不猜、不编分类）。"""
    for kw, label in _EVENT_TYPE_RULES:
        if kw in title:
            return label
    return "资讯"


def _event_card_payload(news_rows: list):
    """个股资讯行 -> 前端 event_card 载荷（时间轴）。

    数据源只有标题 / 正文 / 来源 / 链接，事件类型按标题关键词确定性归纳，
    不做任何语义推断（与项目「数值与分类都来自工具数据」的约定一致）。
    """
    events = []
    for row in news_rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        when = str(row.get("datetime") or "")
        desc = " ".join(str(row.get("content") or "").split())
        etype = _event_type(title)
        events.append({
            "type": etype,
            "date": when[:10],
            "title": title,
            "desc": desc[:_EVENT_DESC_CHARS],
            "important": etype in _EVENT_IMPORTANT_TYPES,
            "source_url": row.get("url") or "",
            "source_name": row.get("channels") or row.get("src") or "",
        })
    if not events:
        return None
    return {
        "events": events,
        # 资讯工具只返回当日新闻（见 tools/news.py 的时间过滤），如实标注口径
        "range": f"当日 · {len(events)} 条",
        "note": "事件来源：个股资讯（当日）",
    }


def _kline_rows(data: list) -> list:
    """日线数据折成前端 kline_table 契约的行结构，按日期升序。

    字段映射：trade_date -> date、vol -> volume（前端 klineCard 读 volume 画量），
    其余开高低收同名透传；行数由工具层收口在 15 个交易日内。
    """
    rows = [r for r in _rows_of(data) if r.get("trade_date")]
    rows.sort(key=lambda r: str(r.get("trade_date", "")))
    return [{
        "date": r.get("trade_date"),
        "open": r.get("open"),
        "high": r.get("high"),
        "low": r.get("low"),
        "close": r.get("close"),
        "volume": r.get("vol"),
    } for r in rows]


def _rows_of(data):
    """把工具载荷归一成行列表：数据源给数组，组合工具给 dict。"""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return [data] if isinstance(data, dict) else []


def _split_net_wan(net):
    """净额（元）-> (买入, 卖出) 万元伪拆分。

    东财资金流只给净额；前端卡片按「买入-卖出」计算净额，
    正数全记买入、负数全记卖出可无损还原净额，且不编造不存在的双边数据。
    """
    if net is None:
        return None, None
    w = round(net / 1e4, 2)
    return (w, 0.0) if w >= 0 else (0.0, -w)


def _fundflow_card_payload(row: dict, extra: dict | None = None) -> dict:
    """资金流工具行（净额，元口径）-> 前端 fundflow_card 载荷（万元口径）。

    字段名与原 Tushare moneyflow 契约保持一致（net_mf_amount、
    buy/sell_{elg,lg,md,sm}_amount），前端卡片工厂无需感知数据源切换。
    """
    main = row.get("main_net")
    payload = {
        "ts_code": row.get("ts_code"),
        "trade_date": row.get("trade_date"),
        "net_mf_amount": round(main / 1e4, 2) if main is not None else None,
    }
    for tier, src in (("elg", "super_net"), ("lg", "large_net"),
                      ("md", "mid_net"), ("sm", "small_net")):
        buy, sell = _split_net_wan(row.get(src))
        payload[f"buy_{tier}_amount"] = buy
        payload[f"sell_{tier}_amount"] = sell
    payload.update(extra or {})
    return payload


# 命中这些词说明用户就是要数据；此时若一个数据都没取到，绝不能让模型自由发挥。
# 除行情价格类外，补上估值/财报/资金/龙虎榜/同业对比五类：它们对应各自的
# 工具与卡片，取不到数时同样必须走兜底文案，而不是让模型凭印象编数字。
# 注意：北向/南向/融资融券对应的工具已下线（2026-10-22），这些词留在表里
# 只会让问句进取数分支后无工具可调、命中兜底文案——属死路由，已清除
_DATA_KEYWORDS = (
    "k线", "K线", "行情", "价格", "涨跌", "走势", "收盘", "报价", "涨幅",
    "估值", "市盈率", "市净率", "市销率", "股息", "市值",
    "财报", "业绩", "营收", "净利润", "毛利", "ROE", "EPS",
    "资金", "主力", "龙虎榜", "流入", "流出",
    "同业", "同行", "竞品", "对比", "新闻", "动态", "消息",
)

def _no_data_fallback(state: dict, tool_results: list) -> str:
    """本轮没有取到任何行情数据、且用户就是要数据时，返回固定兜底文案。

    实测里最常见的坏答案：模型只搜到股票代码就作答，数字全部来自上一轮历史
    （行情是实时的，照抄即过期数据），甚至凭空编出别的标的。提示词规则拦不住，
    这里改成确定性兜底——宁可说「没取到」，也不给过期/编造的数字。
    """
    # 有任一取数类工具成功 -> 数据到手，交给模型作答
    if any(
        item.get("tool") in _DATA_TOOLS and (item.get("result") or {}).get("ok")
        for item in tool_results
    ):
        return ""
    # 模型调过取数类工具却全部失败 -> 用户问的就是数据，必然要兜底
    called_data_tool = any(item.get("tool") in _DATA_TOOLS for item in tool_results)
    if called_data_tool:
        reason = _failure_reason(tool_results)
        # 客观无数据（如近期未上榜、当日暂无新闻）是数据源的真实结论，
        # 不是故障：如实转述即可，套「请稍后重试」会误导用户（实测 L-01）
        if _all_failures_graceful_empty(tool_results):
            return (f"查询完成：{reason or '暂无相关数据'}。"
                    "这是数据源当前的客观情况，并非系统故障；可以换个标的或时间范围再试。")
        head = "抱歉，这一轮没有取到所需的数据，我无法给出你要的数字。"
        if reason:
            head += reason + "。"
        return head + "请稍后重试；也可以带上股票代码再问，例如「000568 最近15个交易日的日K」。"
    # 没调取数工具：可能是知识型问题（如「解释一下 PE 和 PB 的区别」），
    # 这类问题本来就不需要真实数据，兜底文案接管会把一条能答好的回答
    # 替换成无意义的取数失败提示（实测：PE/PB/PS 通俗解释被误伤）
    if _is_concept_question(state):
        return ""
    if _asked_for_data(state):
        return ("抱歉，这一轮没有取到所需的数据，我无法给出你要的数字。"
                "请稍后重试；也可以带上股票代码再问，例如「000568 最近15个交易日的日K」。")
    return ""


# 英文缩写单独匹配：改写步骤常把「估值水平」缩成「PE 和 PB」，中文关键词表里没有这些缩写，
# 只看中文关键词会漏判（漏判后模型会自己编失败原因，把「频率超限(1次/分钟)」说成「每小时限1次」）。
_DATA_ABBR_RE = re.compile(r"(?<![A-Za-z])(PE|PB|PS|ROE|EPS|TTM)(?![A-Za-z])")


def _asked_for_data(state: dict) -> bool:
    """用户这一轮是不是在要数据（原始问题与改写后的问题都算）。"""
    text = ((state.get("query") or "") + " " + (state.get("rewritten") or "")).upper()
    if any(k.upper() in text for k in _DATA_KEYWORDS):
        return True
    return bool(_DATA_ABBR_RE.search(text))


# 客观无数据的失败文案特征：未上榜 / 当日暂无 / 没有相关记录。
# 与「连接失败 / 限频 / 超时」分开：前者是数据源的真实结论，不该套重试话术。
_GRACEFUL_EMPTY_RE = re.compile(r"未上榜|暂无|当日无|没有相关|无记录|无数据")


def _all_failures_graceful_empty(tool_results: list) -> bool:
    """本轮所有取数类失败是否都是「客观无数据」而非「取数失败」。"""
    fails = [item for item in tool_results
             if item.get("tool") in _DATA_TOOLS and not (item.get("result") or {}).get("ok")]
    if not fails:
        return False
    return all(
        _GRACEFUL_EMPTY_RE.search(str((item.get("result") or {}).get("error") or ""))
        for item in fails
    )


# 概念性问题：问的是「是什么 / 怎么算 / 有什么区别」，本身不需要真实数据。
# 这类问题不能被「没取到数据」的兜底文案接管——否则会把一条本来能答好的
# 知识型回答（实测：PE/PB/PS 通俗解释）换成无意义的取数失败提示。
# 注意「对比」不在表里：它已移入 _DATA_KEYWORDS——「A 和 B 对比」要取当日
# 快照出对比卡；「区别/不同」足以覆盖「PE 和 PB 有什么不同」这类概念问法
_CONCEPT_KEYWORDS = (
    "解释", "含义", "意思", "区别", "不同", "为什么", "怎么算", "如何算",
    "怎么用", "如何用", "是什么", "什么是", "通俗", "简单说", "怎么理解",
    "如何理解", "讲讲", "介绍", "科普", "定义", "有什么区别",
)


def _is_concept_question(state: dict) -> bool:
    """这一轮是不是纯概念/知识型问题（不需要真实行情数据）。"""
    text = (state.get("query") or "") + " " + (state.get("rewritten") or "")
    return any(k in text for k in _CONCEPT_KEYWORDS)


def _failure_reason(tool_results: list) -> str:
    """从失败的工具结果里提炼一句准确的失败原因。

    直接把上游原文丢给模型，它会自由发挥出「每小时限1次」这种假细节
    （实测），所以这里归一化成一句准确的中文说明，由兜底文案与提示词共用。
    """
    for item in tool_results:
        res = item.get("result") or {}
        if res.get("ok"):
            continue
        err = str(res.get("error") or "")
        if not err:
            continue
        low = err.lower()
        if "频率" in err or "频繁" in err or "rate" in low:
            return "数据源访问频率受限，请稍后再试"
        if "权限" in err or "没有接口" in err or "forbidden" in low or "403" in low:
            return "数据源暂无法提供该数据（接口权限或数据范围受限）"
        # 连接/登录类判定排在超时之前：「baostock 登录失败: timeout」本质是连不上，
        # 说成「接口响应超时」会误导用户的重试方向（回归测试固定用例）
        if "retry" in low or "connection" in low or "连接" in err or "登录" in err:
            return "数据源连接失败，请稍后再试"
        if "timeout" in low or "timed out" in low:
            return "数据接口响应超时"
        return err[:80]
    return ""


# 上下文超长时的最终兜底：宁可明确告知，也不要留一条空回答
_OVERFLOW_REPLY = (
    "抱歉，这次的数据量超出了模型能一次消化的上限，我没法给出完整回答。"
    "可以缩小范围再问一次，例如「贵州茅台最近 10 天日K」「宁德时代最新一期财报」。"
)


def _msg_tokens(msg) -> int:
    c = getattr(msg, "content", "")
    return estimate_tokens(c if isinstance(c, str) else str(c))


def _fit_context(messages: list) -> list:
    """按 token 预算裁剪上下文：先丢历史，再截提示词。

    上游模型上下文 163840，超了直接 400（整条回答为空）。这里留足余量提前裁，
    避免再出现「问一句、答一句空」的故障。
    """
    # 廉价下界：字符数不到预算的 30% 时必然没超（最坏 3 token/字），跳过整段编码
    if message_chars(messages) < MAX_INPUT_TOKENS * 0.3:
        return messages
    # 逐条算一次再决定丢几条：原实现在 while 里反复全量重算，每次都重新编码几万 token
    costs = [_msg_tokens(m) for m in messages]
    total = sum(costs)
    if total <= MAX_INPUT_TOKENS:
        return messages
    # 1) 丢最老的历史（0 是系统提示、末尾是本轮问题，都保留）
    out = list(messages)
    idx = 1
    while idx < len(out) - 1 and total > MAX_INPUT_TOKENS:
        total -= costs[idx]
        idx += 1
    out = [out[0]] + out[idx:]
    # 2) 还超就截提示词正文（工具结果段在最前面，截尾保留取数结论）
    if total > MAX_INPUT_TOKENS:
        text = out[0].content if isinstance(out[0].content, str) else str(out[0].content)
        out[0] = SystemMessage(content=text[:max(2000, len(text) // 2)] + "\n…[上下文已裁剪]")
    return out


# 进 prompt 前的字段级瘦身参数：资讯正文/研报摘要动辄上千字，
# 整段截尾会把后面有用的取数结论一起挤掉
_SLIM_STR_CHARS = 300
_SLIM_LIST_ITEMS = 20


def _slim_value(value):
    if isinstance(value, str):
        return value if len(value) <= _SLIM_STR_CHARS else value[:_SLIM_STR_CHARS] + "…"
    if isinstance(value, list):
        return [_slim_value(v) for v in value[:_SLIM_LIST_ITEMS]]
    if isinstance(value, dict):
        return {k: _slim_value(v) for k, v in value.items()}
    return value


def _slim_tool_results(tool_results: list) -> list:
    """只影响进 prompt 的文本；卡片发射仍用原始 tool_results。"""
    slim = []
    for item in tool_results:
        r = item.get("result") or {}
        entry = {"tool": item.get("tool"), "ok": r.get("ok"), "data": _slim_value(r.get("data"))}
        if r.get("error"):
            entry["error"] = r["error"]
        slim.append(entry)
    return slim


async def synthesize(state: dict) -> dict:
    tool_results = state.get("tool_results", []) or []
    fallback = _no_data_fallback(state, tool_results)
    if fallback:
        # 不走 LLM：兜底文案必须显式 emit 成 token。main.py 只透传 custom 事件，
        # 返回的 AIMessage 不会自动变成前端文本——少这一句前端就是空白回答（实测）
        emit("token", {"delta": fallback})
        return {"messages": [AIMessage(content=fallback)]}
    # 无工具结果（模型直接回答）时也给一个空上下文
    tr_text = (json.dumps(_slim_tool_results(tool_results), ensure_ascii=False, default=str)
               if tool_results else "（无工具数据）")
    # 同样防超限：用户要长区间 K 线时这段 JSON 会很大，截尾保留取数结论足够作答
    if len(tr_text) > _MAX_TOOL_CHARS:
        tr_text = tr_text[:_MAX_TOOL_CHARS] + "\n…[工具结果已截断]"
    # 失败原因单独点名：直接把原文丢给模型，它会把「频率超限(1次/分钟)」
    # 转述成「每小时限1次」这种假细节，这里给一句准确的中文说明让它照着转述
    reason = _failure_reason(tool_results)
    if reason:
        tr_text = f"【数据获取失败说明】{reason}\n{tr_text}"
    query = state.get("rewritten") or state.get("query", "") or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = render("stock.xml", now=now, query=query, tool_results=tr_text,
                    examples=load_template("stock_examples.xml"))

    # 结构化数据走独立事件
    _emit_structured(tool_results)

    # 带上最近对话历史，让回答能衔接上下文
    messages = [SystemMessage(content=prompt)]
    messages.extend(_recent_history(state))
    messages.append(HumanMessage(content=state.get("query", "") or query))

    # 上下文预算：超了先裁剪再发，绝不把超长请求丢给上游
    messages = _fit_context(messages)

    llm = get_llm(streaming=True, temperature=0.4)
    try:
        parts = []
        async for chunk in llm.astream(messages):
            if isinstance(chunk.content, str) and chunk.content:
                emit("token", {"delta": chunk.content})
            parts.append(chunk)
    except Exception as e:  # noqa: BLE001
        if not is_context_overflow(e):
            raise
        # 上游仍报超长：降级成一条能用的回答，绝不让整条对话只剩 error 事件
        logger.warning("synthesize context overflow, fallback to safe reply", exc_info=True)
        emit("token", {"delta": _OVERFLOW_REPLY})
        return {"messages": [AIMessage(content=_OVERFLOW_REPLY)]}
    ai = parts[0] if parts else None
    for p in parts[1:]:
        ai = ai + p
    return {"messages": [ai if ai is not None else AIMessage(content="")]}


def build_stock_subgraph():
    g = StateGraph(AgentState)
    g.add_node("tool_call", tool_call)
    g.add_node("tool_execute", tool_execute)
    g.add_node("synthesize", synthesize)

    g.add_edge(START, "tool_call")
    g.add_conditional_edges("tool_call", route_after_call, {
        "tool_execute": "tool_execute",
        # 回到 tool_call：模型一轮没调工具但用户要的是数据时，再给一次机会
        # （少了这一条会直接 KeyError: 'tool_call'，整轮对话变成 error）
        "tool_call": "tool_call",
        "synthesize": "synthesize",
    })
    # 执行完工具回到 tool_call：让模型基于工具结果决定「继续调」还是「收尾作答」（多跳 ReAct）
    g.add_edge("tool_execute", "tool_call")
    g.add_edge("synthesize", END)
    # 子图不单独做 checkpoint：无 interrupt、也不跨调用记忆状态，
    # 关掉后省掉每个 superstep 一份全量 state 的落库（父图仍记录整体状态）
    return g.compile(checkpointer=False)
