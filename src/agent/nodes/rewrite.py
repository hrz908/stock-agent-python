"""改写节点：指代消解，把「依赖上下文的问题」改写成独立完整的问题。

主图已改用 nodes/supervisor.py（一次小模型调用同时完成改写 + 路由）。本模块的
`_history_text` / `_get_summary` / `_MERGE_SYSTEM` / `_MERGE_RULE` 仍被 supervisor
复用，`rewrite` 函数保留给回退路径与单测（P17/P23），改口径时两处要一起看。


例：历史在聊贵州茅台，用户问「那它的市盈率呢」→「贵州茅台的市盈率是多少」。
用 small_llm（温度 0）；若 Redis 有该 thread 的滚动摘要（post 节点写入），
则拼入 prompt 作为对话背景，与最近历史共同构成完整上下文。
改写失败 / 无历史无摘要时回退原始 query，绝不阻断主流程。

多轮且确实需要改写时，改写与意图路由合并成一次小模型调用（rewritten + intent），
省掉 classify 的第二次往返；单轮（无历史）或问题本身完整时仍只做改写/直通，
路由照旧交给 classify。合并调用失败自动回退到「只改写」，不改变任何降级路径。
"""
import re
import time
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.core.keywords import fast_chat, fast_intent, fast_off_topic, hit_data_keyword
from agent.core.llm import get_small_llm
from agent.core.redis import get_redis
from agent.core.template import load_template, render
from agent.rag.entity import entity_mentions

# 与 nodes/classify.py 的低置信度口径保持一致（低于该值强制 clarify）
_CONF_MIN = 0.6
_ROUTE_INTENTS = {"stock", "chat", "off_topic", "clarify"}

# 依赖上下文的指代词：出现这些才真的需要消解。
# 反过来说，一个不含指代、且自带 6 位代码的问题（「600519 现在多少钱」）
# 本身就是独立完整的，绕一圈小模型只会白白多 0.5-1.5s。
_ANAPHORA_RE = re.compile(
    r"(它|他|她|其|该股|这只|那只|这个|那个|这家|那家|上述|前面|刚才|继续|再|还)"
)
_CODE_RE = re.compile(r"\d{6}")

_SYSTEM = (
    "你是查询改写器。根据对话历史，把用户最新的问题改写成一个"
    "不依赖上下文、可独立理解的完整问题。要求：\n"
    "1. 只输出改写后的问题本身，不要解释、不要引号、不要前缀。\n"
    "2. 补全指代（它/这个/那只股票等）为具体实体。\n"
    "3. 若最新问题本身已完整，原样输出。\n"
    "4. 保持与用户相同的语言。"
)

# 合并路径：一次调用同时产出改写与路由。规则仍复用 router.xml / router_examples.xml，
# 只在末尾追加改写要求，避免维护两套意图口径。
_MERGE_SYSTEM = (
    "你是股票投研助手的查询改写器与意图路由器。先结合对话历史改写指代，"
    "再按给定规则判断意图，只输出结构化字段。"
)
_MERGE_RULE = (
    "\n\n<rewrite_rule>\n"
    "在判断意图之前，先结合 <history> 把 <query> 改写成一个不依赖上下文、"
    "可独立理解的问题，写入 rewritten 字段。要求：补全指代（它/这个/那只股票等）"
    "为具体实体；若最新问题本身已完整，rewritten 原样使用 <query> 的内容；"
    "保持与用户相同的语言。\n"
    "</rewrite_rule>"
)


class _RewriteRoute(BaseModel):
    """改写 + 路由的合并输出契约（字段与 router.xml 的 route 一致，另加 rewritten）。"""

    rewritten: str = Field(description="改写后不依赖上下文的独立问题")
    intent: str = Field(description="stock|chat|off_topic|clarify 之一")
    confidence: float = Field(description="对该判断的把握，0~1 的小数")
    reason: str = Field(default="", description="一句话理由")
    symbol: str = Field(default="", description="标的代码或名称，可空")
    market: str = Field(default="", description="SH|SZ|BJ|HK|US，可空")
    date_range: str = Field(default="", description="时间范围描述，可空")


# with_structured_output 每次都会重新包装 schema，缓存这个 runnable
_merge_runnable = None


def _get_merge_runnable():
    global _merge_runnable
    if _merge_runnable is None:
        _merge_runnable = get_small_llm(
            streaming=False, temperature=0.0
        ).with_structured_output(_RewriteRoute)
    return _merge_runnable


def _format_history(messages: list, limit: int = 10) -> str:
    """把最近若干条消息压成 'role: text' 文本，供小模型理解上下文。"""
    lines = []
    for m in messages[-limit:]:
        if isinstance(m, HumanMessage):
            role = "用户"
        elif isinstance(m, AIMessage):
            role = "助手"
        else:
            continue
        content = m.content if isinstance(m.content, str) else str(m.content)
        if content.strip():
            lines.append(f"{role}: {content.strip()}")
    return "\n".join(lines)


def _history_text(state: dict) -> str:
    """本轮之前的历史；末尾就是当前问题，不能算作「有历史」。

    guardrail 写入的 messages 恒含本轮 HumanMessage，若直接格式化会把当前问题
    当成对话历史，导致单轮里出现「它们/还是」也被判为需要改写（实测白跑一次
    改写 + 把概念问题误判 clarify）。
    """
    msgs = list(state.get("messages") or [])
    query = (state.get("query") or "").strip()
    if msgs and isinstance(msgs[-1], HumanMessage):
        last_content = msgs[-1].content
        last = last_content if isinstance(last_content, str) else str(last_content or "")
        if not query or last.strip() == query:
            msgs = msgs[:-1]
    return _format_history(msgs)


async def _get_summary(thread_id: str) -> str:
    """读 Redis 中的滚动摘要（post 节点写入）；失败静默返回空。"""
    if not thread_id:
        return ""
    try:
        summary = await get_redis().get(f"agent:summary:{thread_id}")
    except Exception:  # noqa: BLE001 - 摘要不可用不影响改写
        return ""
    if not summary:
        return ""
    return summary.decode() if isinstance(summary, bytes) else summary


async def _needs_rewrite(query: str) -> bool:
    """问题本身是否依赖上下文（保守判据：拿不准就改写）。

    旧判据「≥8 字即完整」会漏掉「最近成交量有放大吗」这类**没有标的、但很长**的
    追问：它跳过改写后拿不到代码，工具只能空取，整轮退化成「请补充股票代码」
    （评测 GMT-04 实测）。现在改为：带 6 位代码 / 命中明确股票名 / 长句且不含
    数据词，三者任一才视为独立问句。
    """
    q = (query or "").strip()
    if not q or _ANAPHORA_RE.search(q):
        return True
    if _CODE_RE.search(q):
        return False
    if len(q) < 8:
        return True
    if not hit_data_keyword(q):
        return False
    return not await entity_mentions(q)


async def rewrite(state: dict) -> dict:
    t0 = time.monotonic()
    query = state.get("query", "") or ""
    history = _history_text(state)
    summary = await _get_summary(state.get("thread_id", ""))
    timings = {"rewrite_ms": int((time.monotonic() - t0) * 1000)}
    # 无历史且无摘要则无需改写
    if not history and not summary:
        return {"rewritten": query, "timings": timings}
    # 问候寒暄没有指代要消解，直接透传并定闲聊意图：合并路由那次 LLM 也一并省掉
    if fast_chat(query):
        return {
            "rewritten": query,
            "intent": "chat",
            "confidence": 1.0,
            "entities": {},
            "timings": timings,
        }
    # 有历史，但问题自己就完整（无指代、带代码/明确标的）：不惊动小模型
    if not await _needs_rewrite(query):
        return {"rewritten": query, "timings": timings}

    # 滚动摘要作为对话背景前缀，弥补被裁剪掉的早期历史
    bg = f"对话背景摘要：\n{summary}\n\n" if summary else ""
    prompt = f"{bg}对话历史：\n{history}\n\n用户最新问题：{query}\n\n改写后的独立问题："

    # 合并路径：一次调用同时产出改写与意图，省掉 classify 的一次往返
    try:
        route_prompt = bg + render(
            "router.xml",
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            history=history,
            query=query,
            examples=load_template("router_examples.xml"),
        ) + _MERGE_RULE
        decision = await _get_merge_runnable().ainvoke([
            SystemMessage(content=_MERGE_SYSTEM),
            HumanMessage(content=route_prompt),
        ])
        text = (decision.rewritten or "").strip().strip('"').strip()
        rewritten = (text.splitlines()[0].strip() if text else query) or query
        # 合并模型有时把指代原样留在 rewritten 里、只把实体填进 symbol 字段
        # （实测「最近成交量有放大吗」→ symbol=比亚迪 但 rewritten 未补全，
        # 下游实体识别拿不到代码，工具只能空取，GMT-04 因此失败）。
        # symbol 存在且 rewritten 里没有它时，直接补进句首。
        symbol = (decision.symbol or "").strip()
        if symbol and symbol not in rewritten:
            rewritten = f"{symbol} {rewritten}".strip()
        intent = decision.intent if decision.intent in _ROUTE_INTENTS else "clarify"
        confidence = float(decision.confidence or 0.0)
        # 关键词兜底：改写后命中取数/文档词时以确定性口径为准，防止合并路由
        # 把「它的市盈率」判回 RAG，白跑一次注定失败的检索
        fast = fast_intent(rewritten)
        if fast:
            intent, confidence = fast, 1.0
        elif fast_off_topic(rewritten):
            intent, confidence = "off_topic", 1.0
        elif confidence < _CONF_MIN:
            intent, confidence = "clarify", 0.0
        return {
            "rewritten": rewritten,
            "intent": intent,
            "confidence": confidence,
            "entities": {
                "symbol": decision.symbol or "",
                "market": decision.market or "",
                "date_range": decision.date_range or "",
            },
            "timings": {"rewrite_ms": int((time.monotonic() - t0) * 1000)},
        }
    except Exception:  # noqa: BLE001 - 合并失败退回「只改写」，不阻断主流程
        pass

    try:
        llm = get_small_llm(streaming=False, temperature=0.0)
        resp = await llm.ainvoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=prompt),
        ])
        text = (resp.content or "").strip().strip('"').strip()
        # 空结果或多行异常时回退原始 query
        rewritten = text.splitlines()[0].strip() if text else query
        timings["rewrite_ms"] = int((time.monotonic() - t0) * 1000)
        return {"rewritten": rewritten or query, "timings": timings}
    except Exception:  # noqa: BLE001 - 改写失败不影响主流程
        timings["rewrite_ms"] = int((time.monotonic() - t0) * 1000)
        return {"rewritten": query, "timings": timings}
