#!/usr/bin/env python
"""AI 对话故障的回归测试（纯函数，不依赖 LLM / 数据源 / 网络）。

覆盖 2026-10-22 实测发现的三个问题：
  P1 上下文溢出（301046 token > max_seq_len）导致整条回答为空
  P2 取数类接口返回整段历史，把对话上下文撑爆
  P3 无数据兜底靠关键词判定，漏判后模型自己编失败原因
2026-10-23 LangGraph 架构改造新增：
  P5 意图三分（stock/chat/off_topic）+ rag 两级命中判读 + 固定话术婉拒
  P6 卡片 pydantic 校验（失败降级纯文本）
  P7 进程内工具集（工具名 / 标签 / 知识库检索工具齐全）
  P8 benchmark 调优新增行为（改写贯通 tool_call / 含糊指判 clarify / 兜底分级 / few-shot）
  P9 会话历史与 checkpoint 增长治理（消息窗口裁剪 / thread_id 口径 / session_id 校验）
2026-10-26 新增：
  P11 卡片选择（一次回复最多一张，K 线话题发 K 线卡）
  P12 正文不追加免责声明
  P17 回答版式规范（分段 / 标点 / 不用加粗）
  P18 闲聊分支不再把问句当回答（提示词结构 + 流式标签净化）+ 行情问句不被概念词误判

用法：python tests/test_regression.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from config import MAX_INPUT_TOKENS
from core.keywords import fast_chat, fast_intent
from core.llm import count_message_tokens, is_context_overflow
from core.sanitize import StreamCleaner
from nodes.refuse import _REFUSE_TEXT, refuse
from subgraphs.cards import validate_card, validate_table
from subgraphs.stock import (
    _DATA_ABBR_RE,
    _OVERFLOW_REPLY,
    _TOOL_LABELS,
    _asked_for_data,
    _call_rounds,
    _event_desc,
    _failure_reason,
    _fit_context,
    _got_data,
    _is_concept_question,
    _needs_data_fetch,
    _no_data_fallback,
    _normalize_tool_call,
    _parse_text_tool_call,
    _pending_nudge,
    _recent_history,
    route_after_call,
    route_after_execute,
)
from tools import take_latest, tool_cache_clear
from tools.rag_tool import search_knowledge_base

_FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _FAILED.append(name)


def _tool_results(name, ok, error=None, data=None):
    res = {"ok": ok}
    if not ok and error:
        res["error"] = error
    if data is not None:
        res["data"] = data
    return [{"tool": name, "result": res}]


# ===== P1：上下文溢出 =====
def test_overflow_detection():
    print("\n[P1] 上下文溢出识别")
    real = ("OpenAIInvalidRequestError: Error code: 400 - {'code': 20015, 'message': "
            "'number of input tokens (301046) has exceeded max_seq_len (163840) limit.'}")
    check("识别真实上游报错", is_context_overflow(Exception(real)))
    check("普通异常不误判", not is_context_overflow(Exception("connection reset")))
    check("兜底文案非空", bool(_OVERFLOW_REPLY.strip()))


def test_fit_context():
    print("\n[P1] 上下文预算裁剪")
    huge = "x" * 600_000
    msgs = [
        SystemMessage(content=huge),
        HumanMessage(content="上一轮问题"),
        AIMessage(content="上一轮回答"),
        HumanMessage(content="本轮问题"),
    ]
    out = _fit_context(msgs)
    n = count_message_tokens(out)
    check(f"裁剪后 token <= 预算（{n} <= {MAX_INPUT_TOKENS}）", n <= MAX_INPUT_TOKENS)
    check("保留了本轮问题",
          any(isinstance(m, HumanMessage) and m.content == "本轮问题" for m in out))
    check("原列表未被就地修改", msgs[0].content == huge)


def test_recent_history_drops_tool_messages():
    print("\n[P1] 工具结果不进 LLM 上下文")
    big = '{"ok": true, "data": [' + ",".join(['{"pe": 1.2}'] * 5000) + ']}'
    state = {
        "query": "贵州茅台的PE是多少",
        "messages": [
            HumanMessage(content="贵州茅台的PE是多少"),
            AIMessage(content="", tool_calls=[{"name": "get_valuation", "args": {}, "id": "1"}]),
            ToolMessage(content=big, tool_call_id="1", name="get_valuation"),
            AIMessage(content="我查一下"),
        ],
    }
    hist = _recent_history(state)
    check("不含 ToolMessage", not any(isinstance(m, ToolMessage) for m in hist))
    check("不含带 tool_calls 的 AIMessage",
          not any(getattr(m, "tool_calls", None) for m in hist))
    check("不含本轮问题（由 synthesize 显式追加）",
          not any(isinstance(m, HumanMessage) and m.content == state["query"] for m in hist))
    total = sum(len(m.content if isinstance(m.content, str) else "") for m in hist)
    check(f"历史体积可控（{total} 字符）", total < 5000)


# ===== P2：取数接口返回整段历史 =====
def test_take_latest():
    print("\n[P2] 取数结果只保留最近 N 条")
    rows = [
        {"trade_date": "20260101", "pe": 1},
        {"trade_date": "20261020", "pe": 2},
        {"trade_date": "20260901", "pe": 3},
    ]
    out = take_latest(rows, "trade_date", 2)
    check("按日期倒序", [r["trade_date"] for r in out] == ["20261020", "20260901"])
    check("不超过 limit", len(out) == 2)
    check("非 list 输入安全", take_latest(None) == [])
    check("缺日期字段不炸", len(take_latest([{"x": 1}], "trade_date", 5)) == 1)


def test_valuation_window():
    print("\n[P2] 估值工具带日期窗口")
    from tools.fundamental import _MAX_VALUATION_ROWS, _date_window
    s, e = _date_window(30)
    check(f"默认窗口为日期区间（{s}~{e}）", len(s) == 8 and len(e) == 8)
    s2, _ = _date_window(99999)
    check("超长区间被夹住", s2 < s, f"{s2} 应早于 {s}")
    check("估值行数上限存在", _MAX_VALUATION_ROWS <= 50)


# ===== P3：无数据兜底 =====
def test_asked_for_data():
    print("\n[P3] 「用户要数据」判定不再漏判缩写")
    st = {"query": "贵州茅台现在的估值水平如何？它当前的PE和PB大概是多少？",
          "rewritten": "贵州茅台的PE和PB是多少？"}
    check("含中文关键词", _asked_for_data(st))
    check("只有英文缩写也判定为要数据",
          _asked_for_data({"query": "它的PE呢", "rewritten": "贵州茅台的PE呢"}))
    check("缩写不误命中普通英文单词", not _DATA_ABBR_RE.search("OPEN TYPE EXPECTED"))
    check("闲聊不算要数据", not _asked_for_data({"query": "你好呀", "rewritten": "你好呀"}))


def test_failure_reason():
    print("\n[P3] 失败原因如实归一化")
    rate = _failure_reason(
        _tool_results("get_valuation", False, "访问频率超限，请稍后再试"))
    check("限频原因准确", "频率" in rate and "稍后" in rate, rate)
    perm = _failure_reason(
        _tool_results("get_financial_indicator", False, "没有接口访问权限"))
    check("权限原因准确", "权限" in perm, perm)
    tmo = _failure_reason(_tool_results("get_quote", False, "timeout"))
    check("超时原因准确", "超时" in tmo, tmo)
    conn = _failure_reason(_tool_results("get_quote", False, "baostock 登录失败: timeout"))
    check("连接原因准确", "连接" in conn, conn)
    check("无失败时为空",
          _failure_reason(_tool_results("get_quote", True, data=[{}])) == "")


def test_no_data_fallback():
    print("\n[P3] 无数据兜底：命中且不编造")
    st = {"query": "贵州茅台现在的估值水平如何？它当前的PE和PB大概是多少？",
          "rewritten": "贵州茅台的PE和PB是多少？"}
    tr = (_tool_results("symbol_search", True, data=[{"code": "600519"}])
          + _tool_results("get_valuation", False, "访问频率超限"))
    out = _no_data_fallback(st, tr)
    check("触发兜底", bool(out), repr(out))
    check("兜底里带真实原因", "频率" in out, out)
    check("兜底不编造「每小时」", "每小时" not in out, out)

    # 有数据拿到时不能兜底（否则会盖掉正常回答）
    tr_ok = _tool_results("get_quote", True, data=[{"price": "1309.69"}])
    check("拿到数据时不兜底", _no_data_fallback(st, tr_ok) == "")

    # 闲聊不兜底
    tr_none = []
    check("闲聊不兜底", _no_data_fallback({"query": "你好", "rewritten": "你好"}, tr_none) == "")

    # 调了取数工具但全失败 -> 一定要兜底（这是实测漏判的路径）
    tr_fail = _tool_results("get_financial_indicator", False, "没有接口访问权限")
    check("取数工具全失败必兜底",
          bool(_no_data_fallback({"query": "看看宁德时代财报", "rewritten": ""}, tr_fail)))


def test_concept_question_not_blocked():
    print("\n[P3] 知识型问题不被兜底误伤")
    # 实测 Q3：问「PE/PB/PS 是什么意思」，模型一个工具都没调，
    # 却被「关键词命中」当成取数问题，兜底文案把一条好答案替换掉了
    st = {"query": "用通俗的语言解释市盈率(PE)、市净率(PB)和市销率(PS)的含义，以及怎么用它们给股票估值。",
          "rewritten": "解释一下市盈率(PE)、市净率(PB)和市销率(PS)的含义"}
    check("识别为概念问题", _is_concept_question(st))
    check("概念问题不兜底", _no_data_fallback(st, []) == "")

    # 概念词 + 缩写同时出现时仍要判为概念问题
    st2 = {"query": "PE 和 PB 有什么区别", "rewritten": ""}
    check("缩写型概念问题", _is_concept_question(st2))
    check("缩写型概念问题不兜底", _no_data_fallback(st2, []) == "")

    # 取数型问题（要具体数值）不能被当成概念问题
    st3 = {"query": "贵州茅台现在的PE和PB是多少", "rewritten": ""}
    check("取数型问题不是概念问题", not _is_concept_question(st3))
    check("取数型问题仍兜底", bool(_no_data_fallback(st3, [])))


def test_data_question_always_fetches():
    print("\n[P3] 要数据的问题一定会去取数（不靠模型自觉）")
    data_q = {"query": "查询贵州茅台（SH600519）的最新行情", "rewritten": ""}
    concept_q = {"query": "解释一下市盈率的含义", "rewritten": ""}

    check("数据问题判定为需要取数", _needs_data_fetch(data_q))
    check("概念问题不需要取数", not _needs_data_fetch(concept_q))

    # 模型一轮没调工具就作答 -> 必须再回 tool_call 一次
    st = {
        "query": data_q["query"], "rewritten": "", "tool_results": [],
        "messages": [HumanMessage(content=data_q["query"]), AIMessage(content="我查一下")],
    }
    check("没调工具时路由回 tool_call", route_after_call(st) == "tool_call")
    check("轮次计数正确", _call_rounds(st) == 1)
    check("取数提示语已生成", "get_quote" in _pending_nudge(st))

    # 第一轮不加提示：实测加了会让模型把「调用」抄进函数名槽位
    st_first = {
        "query": data_q["query"], "rewritten": "", "tool_results": [],
        "messages": [HumanMessage(content=data_q["query"])],
    }
    check("第一轮不追加提示", _pending_nudge(st_first) == "", repr(_pending_nudge(st_first)))
    check("提示语里不出现「调用」二字", "调用" not in _pending_nudge(st))

    # 概念问题不重试，直接合成
    st2 = {
        "query": concept_q["query"], "rewritten": "", "tool_results": [],
        "messages": [HumanMessage(content=concept_q["query"]), AIMessage(content="我来解释")],
    }
    check("概念问题直接合成", route_after_call(st2) == "synthesize")
    check("概念问题不生成取数提示", _pending_nudge(st2) == "")

    # 已经拿到数据 -> 不再重试
    st3 = {
        "query": data_q["query"], "rewritten": "",
        "tool_results": _tool_results("get_quote", True, data=[{"price": "1309.69"}]),
        "messages": [HumanMessage(content=data_q["query"]), AIMessage(content="取到了")],
    }
    check("已取到数据则不重试", route_after_call(st3) == "synthesize")
    check("已取到数据的判定", _got_data(st3))

    # 轮次用尽 -> 合成（不能死循环）
    st4 = {
        "query": data_q["query"], "rewritten": "", "tool_results": [],
        "messages": [HumanMessage(content=data_q["query"])] + [AIMessage(content="x")] * 3,
    }
    check("轮次用尽后合成", route_after_call(st4) == "synthesize")

    # 历史轮次不计入本轮上限（多轮对话不会被提前吃光）
    st5 = {
        "query": data_q["query"], "rewritten": "", "tool_results": [],
        "messages": [AIMessage(content="旧轮次"), AIMessage(content="旧轮次"),
                     HumanMessage(content=data_q["query"]), AIMessage(content="本轮")],
    }
    check("历史轮次不计入", _call_rounds(st5) == 1)


def test_graph_wiring_and_retry():
    print("\n[P3] 子图接线：重试路由可用（防 KeyError）")
    import asyncio

    from subgraphs import stock as stock_mod

    class _FakeLLM:
        """永远不调工具的假模型：逼出「模型没调工具」这条分支。"""

        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, msgs):
            self.calls += 1
            return AIMessage(content=f"第{self.calls}轮：我先想想")

    fake = _FakeLLM()
    orig = stock_mod.get_llm
    orig_plan = stock_mod._fast_plan

    async def _no_plan(state):
        # 本用例只验证「模型没调工具 -> 有界重试」的接线，不能被确定性规划接管
        return None

    stock_mod.get_llm = lambda **kw: fake
    stock_mod._fast_plan = _no_plan
    try:
        g = stock_mod.build_stock_subgraph()
        q = "查询贵州茅台（SH600519）的最新行情"
        state = {
            "messages": [HumanMessage(content=q)],
            "query": q,
            "rewritten": "",
            "tool_results": [],
        }
        out = asyncio.run(g.ainvoke(state))
        check("子图能跑完不报 KeyError", True)
        check(f"触发了重试（LLM 被调用 {fake.calls} 次）", fake.calls >= 2, f"calls={fake.calls}")
        check("轮次有上限（不超过 4 次）", fake.calls <= 4, f"calls={fake.calls}")
        check("最终产出了回答", bool(out.get("messages")))
    except Exception as e:  # noqa: BLE001
        check("子图能跑完不报 KeyError", False, f"{type(e).__name__}: {e}")
    finally:
        stock_mod.get_llm = orig
        stock_mod._fast_plan = orig_plan


def test_tool_short_circuit():
    print("\n[P13] 取数短路：有数据/客观无数据直接收尾")
    ok = {"tool_results": _tool_results("get_quote", True, data=[{"price": 1.0}])}
    check("取到数据 -> synthesize", route_after_execute(ok) == "synthesize", str(route_after_execute(ok)))
    empty = {"tool_results": _tool_results("get_top_list", False, "近期未上榜或暂无龙虎榜数据")}
    check("客观无数据 -> synthesize", route_after_execute(empty) == "synthesize",
          str(route_after_execute(empty)))
    search = {"tool_results": _tool_results("symbol_search", True, data=[{"code": "600519"}])}
    check("只搜到代码 -> 继续 tool_call", route_after_execute(search) == "tool_call",
          str(route_after_execute(search)))
    fail = {"tool_results": _tool_results("get_quote", False, "timeout")}
    check("瞬时失败 -> 允许再决策", route_after_execute(fail) == "tool_call",
          str(route_after_execute(fail)))
    check("空结果 -> 继续 tool_call", route_after_execute({"tool_results": []}) == "tool_call")

    # route_after_call 在客观无数据时不再回 tool_call
    st = {
        "query": "查一下三花智控最近的龙虎榜席位明细",
        "rewritten": "",
        "tool_results": _tool_results("get_top_list", False, "近期未上榜或暂无龙虎榜数据"),
        "messages": [HumanMessage(content="q"), AIMessage(content="x")],
    }
    check("客观无数据不再重试决策", route_after_call(st) == "synthesize",
          str(route_after_call(st)))


def test_graph_short_circuit_after_tool_success():
    print("\n[P13] 子图接线：数据到手后不再多跑 tool_call")
    import asyncio

    from langchain_core.messages import AIMessageChunk

    from subgraphs import stock as stock_mod

    class _FakeTool:
        name = "get_quote"

        async def ainvoke(self, args):
            return {"ok": True, "data": {"code": "600519", "name": "贵州茅台", "price": 1.0}}

    class _FakeToolLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, msgs):
            self.calls += 1
            return AIMessage(content="", tool_calls=[{
                "name": "get_quote", "args": {"code": "600519"}, "id": "1",
            }])

    class _FakeStreamLLM:
        async def astream(self, msgs):
            yield AIMessageChunk(content="已取到数据")

    # 工具结果有进程级缓存：先清掉别的用例留下的失败结论，否则这里会命中缓存、
    # 假工具根本不会被调用（实测表现为「明明 stub 了工具却还在打真实网关」）
    tool_cache_clear()

    tool_llm = _FakeToolLLM()
    orig_tool_bound = stock_mod._tool_bound_llm
    orig_get_llm = stock_mod.get_llm
    orig_tools = stock_mod._TOOLS
    orig_bound = stock_mod._bound_llm
    stock_mod._tool_bound_llm = lambda: tool_llm
    stock_mod.get_llm = lambda **kw: _FakeStreamLLM()
    stock_mod._TOOLS = [_FakeTool()]
    stock_mod._bound_llm = None
    try:
        g = stock_mod.build_stock_subgraph()
        q = "查询贵州茅台（SH600519）的最新行情"
        out = asyncio.run(g.ainvoke({
            "messages": [HumanMessage(content=q)],
            "query": q,
            "rewritten": "",
            "tool_results": [],
            "timings": {},
        }))
        # 代码 + 行情词命中确定性规划：连那一轮决策 LLM 都不跑（首响省掉 1.4~2.8s）
        check("规划命中时不跑决策 LLM", tool_llm.calls == 0, f"calls={tool_llm.calls}")
        check("最终生成回答",
              any("已取到" in (getattr(m, "content", "") or "") for m in out.get("messages", [])),
              str(out.get("messages")))
        check("阶段计时含 tool_call/tool_execute/synth",
              all(k in (out.get("timings") or {})
                  for k in ("tool_call_ms", "tool_execute_ms", "synth_ttft_ms")),
              str(out.get("timings")))

        # 规划拿不准的问句仍回退原来的 LLM 决策（链路没被改坏）
        q2 = "帮我查一下这只票"
        out2 = asyncio.run(g.ainvoke({
            "messages": [HumanMessage(content=q2)],
            "query": q2,
            "rewritten": "",
            "tool_results": [],
            "timings": {},
        }))
        check("规划未命中时仍走 LLM 决策", tool_llm.calls == 1, f"calls={tool_llm.calls}")
        check("回退路径也能出回答",
              any("已取到" in (getattr(m, "content", "") or "") for m in out2.get("messages", [])),
              str(out2.get("messages")))
    finally:
        stock_mod._tool_bound_llm = orig_tool_bound
        stock_mod.get_llm = orig_get_llm
        stock_mod._TOOLS = orig_tools
        stock_mod._bound_llm = orig_bound


def test_stage_timings_and_ttft():
    print("\n[P13] 阶段计时与 TTFT 口径")
    import asyncio

    from app.main import _is_answer_event
    from core.state import initial_state, merge_timings
    from nodes.classify import classify
    from nodes.guardrail import guardrail
    from nodes.rewrite import rewrite

    check("token 算答案事件", _is_answer_event("token"))
    check("card 算答案事件", _is_answer_event("card"))
    check("table 算答案事件", _is_answer_event("table"))
    check("tool_start 不算答案事件", not _is_answer_event("tool_start"))
    check("sources 不算答案事件", not _is_answer_event("sources"))

    st = initial_state("1", "t", "q")
    check("初始 timings 为空", st.get("timings") == {})
    check("timings reducer 合并", merge_timings({"a": 1}, {"b": 2}) == {"a": 1, "b": 2})

    out = asyncio.run(guardrail({"query": "你好", "messages": []}))
    check("guardrail_ms 已记录",
          isinstance(out["timings"].value.get("guardrail_ms"), int), str(out.get("timings")))

    from langgraph.types import Overwrite
    out_reset = asyncio.run(guardrail({
        "query": "你好", "messages": [], "timings": {"tool_call_ms": 9999},
    }))
    check("guardrail 重置上轮 timings",
          isinstance(out_reset.get("timings"), Overwrite)
          and "tool_call_ms" not in out_reset["timings"].value,
          str(out_reset.get("timings")))
    out2 = asyncio.run(rewrite({"query": "你好", "messages": [], "thread_id": ""}))
    check("rewrite_ms 已记录",
          isinstance((out2.get("timings") or {}).get("rewrite_ms"), int), str(out2.get("timings")))
    out3 = asyncio.run(classify({"query": "贵州茅台最新行情", "rewritten": "", "messages": []}))
    check("classify_ms 已记录",
          isinstance((out3.get("timings") or {}).get("classify_ms"), int), str(out3.get("timings")))


def test_zh_tokenize():
    print("\n[P14] 中文 bigram 词元化")
    from rag.zh_tokenize import document_text, query_tsquery

    doc = document_text("贵州茅台发布了回购公告，投资者可关注")
    check("中文按 bigram 切分", "贵州" in doc and "州茅" in doc and "茅台" in doc, doc)
    check("ASCII 代码保留", "600519" in document_text("查询600519最新价"), document_text("查询600519最新价"))
    check("重复 bigram 保留（供 tf 计分）", document_text("茅台茅台") == "茅台 台茅 茅台",
          document_text("茅台茅台"))
    q = query_tsquery("贵州茅台 回购 600519")
    check("查询词元去重 OR 拼接", "茅台 | 回购 | 600519" in q, q)
    check("空文本哨兵", query_tsquery("") == "__empty__")
    check("单字虚词过滤", query_tsquery("的") == "__empty__", query_tsquery("的"))
    # 与 ingest 侧保持一致：两份同源文件必须产出相同词元
    import runpy
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / ".." / "ingest" / "zh_tokenize.py").resolve()
    ns = runpy.run_path(str(src))
    check("ingest 侧词元一致",
          ns["document_text"]("贵州茅台回购") == document_text("贵州茅台回购"))


def test_llm_retry_config():
    print("\n[P13] LLM 超时与重试收口")
    from config import LLM_MAX_RETRIES, SMALL_LLM_TIMEOUT, TOOL_CALL_LLM_TIMEOUT
    from core import llm as llm_mod

    orig_key = llm_mod.OPENAI_API_KEY
    # 构造 ChatOpenAI 需要非空 key；这里只做离线配置检查，不发起请求
    llm_mod.OPENAI_API_KEY = "test-key"
    try:
        llm = llm_mod.get_llm(streaming=False, timeout=TOOL_CALL_LLM_TIMEOUT)
        check("max_retries 已收口", llm.max_retries == LLM_MAX_RETRIES,
              f"{llm.max_retries} vs {LLM_MAX_RETRIES}")
        check("tool_call 超时生效", llm.request_timeout == TOOL_CALL_LLM_TIMEOUT,
              f"{llm.request_timeout} vs {TOOL_CALL_LLM_TIMEOUT}")
        small = llm_mod.get_small_llm()
        check("小模型默认 30s", small.request_timeout == SMALL_LLM_TIMEOUT,
              f"{small.request_timeout} vs {SMALL_LLM_TIMEOUT}")
    finally:
        llm_mod.OPENAI_API_KEY = orig_key
        llm_mod._LLM_CACHE.clear()


def test_text_tool_call_restore():
    print("\n[P4] 文本形态的工具调用能被还原成结构化调用")
    txt = 'function\nget_quote\n```json\n{"code":"SH600519"}\n```'
    name, args = _parse_text_tool_call(txt)
    check("解析出工具名", name == "get_quote", str(name))
    check("解析出参数", args == {"code": "SH600519"}, str(args))

    name2, args2 = _parse_text_tool_call('function\nget_money_flow\n{"code":"600519","days":30}')
    check("无 json 围栏也能解析", name2 == "get_money_flow" and args2.get("days") == 30, str(args2))

    name3, _ = _parse_text_tool_call("今天天气不错，我们来聊聊市盈率")
    check("普通文本不误判", name3 is None, str(name3))

    name4, _ = _parse_text_tool_call('function\nnot_a_real_tool\n{"a":1}')
    check("未注册的工具名不认", name4 is None, str(name4))

    # 实测形态：提示里写了「必须调用 get_quote」，模型把「调用」抄进了函数名槽位
    txt2 = 'function调用\n```json\n{"code":"SH600519"}\n```'
    n5, a5 = _parse_text_tool_call(txt2)
    check("函数名被写坏时按参数反推", n5 == "get_quote", str(n5))
    check("反推时参数仍正确", a5 == {"code": "SH600519"}, str(a5))

    n6, a6 = _parse_text_tool_call('function调用\n```json\n{"code":"600519","count":10}\n```')
    check("K 线工具已下线，count 不再反推", n6 is None, str(n6))

    n7, _ = _parse_text_tool_call('```json\n{"query":"贵州茅台"}\n```')
    check("带 query 反推为搜索", n7 == "symbol_search", str(n7))

    out = _normalize_tool_call(AIMessage(content=txt))
    check("归一化后有 tool_calls", len(out.tool_calls) == 1, str(out.tool_calls))
    check("归一化后正文清空", out.content == "")

    ai2 = AIMessage(content="", tool_calls=[{"name": "get_quote", "args": {"code": "1"}, "id": "x"}])
    out2 = _normalize_tool_call(ai2)
    check("已有结构化调用时不改动",
          len(out2.tool_calls) == 1 and out2.tool_calls[0]["id"] == "x")


def test_card_validation():
    print("\n[P6] 卡片 pydantic 校验（失败降级为纯文本）")
    check("行情卡通过", validate_card("quote_card", {"code": "600519", "price": 1300.0}))
    check("行情卡缺标识被拦", not validate_card("quote_card", {"price": 1300.0}))
    check("行情卡无价格被拦", not validate_card("quote_card", {"code": "600519"}))
    check("估值卡通过", validate_card("valuation_card",
                                    {"ts_code": "600519.SH", "trade_date": "20261023", "pe": 30}))
    check("估值卡缺日期被拦", not validate_card("valuation_card", {"ts_code": "600519.SH"}))
    check("资金流卡通过", validate_card("fundflow_card", {"ts_code": "600519", "net_mf_amount": 1.0}))
    check("纯龙虎榜卡通过", validate_card("fundflow_card",
                                     {"lhb": [{"seat": "x", "buy": 1, "sell": 2}],
                                      "lhb_date": "20261022"}))
    check("资金流卡空载荷被拦", not validate_card("fundflow_card", {}))
    check("财报卡通过", validate_card("report_card",
                                   {"income": {"revenue": 1}, "indicator": None, "cashflow": None}))
    check("财报卡全空被拦", not validate_card("report_card",
                                       {"income": None, "indicator": None, "cashflow": None}))
    check("对比卡通过", validate_card("compare_card", {"items": [{"name": "x"}]}))
    check("对比卡空 items 被拦", not validate_card("compare_card", {"items": []}))
    check("K线表通过", validate_table("kline_table",
                                  {"rows": [{"date": "2026-10-23", "close": 1}]}))
    check("K线表缺日期被拦", not validate_table("kline_table", {"rows": [{"close": 1}]}))
    check("搜索候选通过", validate_table("symbol_list", [{"code": "600519", "name": "贵州茅台"}]))
    check("搜索候选缺 code 被拦", not validate_table("symbol_list", [{"name": "贵州茅台"}]))


# ===== P11：卡片选择（2026-10-26）=====
def test_card_selection():
    print("\n[P11] 一次回复只发一张卡，K 线话题发 K 线卡")
    from subgraphs import stock as stock_mod

    emitted = []
    orig_card, orig_table = stock_mod.emit_card, stock_mod.emit_table
    stock_mod.emit_card = lambda t, p: emitted.append(t)
    stock_mod.emit_table = lambda t, p: emitted.append(t)

    quote = ("get_quote", {"code": "SH600519", "name": "贵州茅台",
                           "price": 1500.0, "change_percent": 1.2})
    valuation = ("get_valuation",
                 [{"ts_code": "600519.SH", "trade_date": "20261023", "pe": 30}])
    flow = ("get_money_flow",
            [{"ts_code": "600519.SH", "trade_date": "20261023", "main_net": 1e8,
              "super_net": 6e7, "large_net": 4e7, "mid_net": -3e7, "small_net": -7e7}])
    report = ("get_financial_report",
              {"income": {"revenue": 1}, "indicator": None, "cashflow": None})
    history = ("get_history_daily",
               [{"trade_date": "2026-10-23", "open": 1, "high": 2,
                 "low": 0.5, "close": 1.5, "vol": 100}])
    news = ("get_stock_news",
            {"rows": [{"title": "茅台回购公告", "datetime": "2026-10-23 10:00"}]})
    search = ("symbol_search", [{"code": "600519", "name": "贵州茅台", "market": "SH"}])

    def run(query, tools):
        emitted.clear()
        tr = [{"tool": t, "result": {"ok": True, "data": d}} for t, d in tools]
        stock_mod._emit_structured({"query": query, "rewritten": ""}, tr)
        return list(emitted)

    try:
        # K 线（蜡烛图）话题：发 K 线卡，哪怕同时取到了行情也压掉行情卡
        check("K线问句只发K线卡", run("贵州茅台最近15个交易日的日K走势怎么样？",
                                [quote, history]) == ["kline_table"])
        # 本轮没取到 K 线数据时仍一张都不发（不拿行情卡顶替）
        check("K线问句无K线数据时不发卡", run("看一下茅台的 K 线图和技术形态",
                                        [quote]) == [])

        # 核心意图 -> 只发对应的那一张（候选里即使有行情卡也压掉）
        check("估值问句只发估值卡", run("贵州茅台现在的估值水平如何？PE 和 PB 大概多少",
                                  [quote, valuation, flow]) == ["valuation_card"])
        check("资金问句只发资金流卡", run("最近比亚迪的主力资金流向怎么样？是净流入还是流出",
                                    [quote, flow]) == ["fundflow_card"])
        check("财报问句只发财报卡", run("看一下茅台最新的财报主要指标",
                                  [quote, report]) == ["report_card"])
        check("行情问句只发行情卡", run("贵州茅台现在什么价", [quote, news]) == ["quote_card"])
        check("新闻问句只发事件卡", run("茅台最近有什么新闻动态",
                                  [quote, news]) == ["event_card"])

        # 没有关键词证据：按工具调用顺序取第一个，数量仍然是一张
        check("无命中时只发一张（工具顺序第一个）",
              run("贵州茅台", [quote, news]) == ["quote_card"])

        # 搜索候选只是中间结果，不参与发卡
        check("搜索中间结果不发卡", run("查一下茅台", [search]) == [])

        # 首选卡载荷不合规 -> 顺延到下一张，不空手也不发两张
        check("首选卡校验失败时顺延",
              run("茅台估值高不高",
                  [("get_valuation", [{"ts_code": "600519.SH"}]), quote]) == ["quote_card"])
    finally:
        stock_mod.emit_card, stock_mod.emit_table = orig_card, orig_table


# ===== P12：正文不再追加免责声明（2026-10-26）=====
def test_no_disclaimer():
    print("\n[P12] 收尾节点不改写正文、不追加免责声明")
    import asyncio

    from nodes.post import post

    ai = AIMessage(content="贵州茅台最新价 1500 元，涨 1.2%。")
    out = asyncio.run(post({
        "intent": "stock",
        "messages": [HumanMessage(content="q"), ai],
    }))
    check("post 不再改写 messages", "messages" not in out, str(list(out.keys())))
    check("只回用量统计", "usage" in out, str(list(out.keys())))


def test_off_topic_refusal():
    print("\n[P5] off_topic 固定话术婉拒")
    import asyncio

    out = asyncio.run(refuse({"query": "帮我写一首诗"}))
    msg = out["messages"][0]
    check("产出固定话术", msg.content == _REFUSE_TEXT)
    check("话术含身份与引导", "溜溜" in msg.content and "股票" in msg.content)
    check("婉拒不反问", "？" not in msg.content and "?" not in msg.content)


def test_rag_judge_fallback():
    print("\n[P5] RAG 两级命中判读")
    import asyncio

    from subgraphs import rag as rag_mod

    orig_ent = rag_mod.entity_codes

    async def _no_entity(_q):
        return set()

    rag_mod.entity_codes = _no_entity
    out = asyncio.run(rag_mod.sufficiency_check({"retrieved": [], "query": "x"}))
    check("空检索 -> stock_agent", out.get("intent") == "stock_agent", str(out))
    out_concept = asyncio.run(rag_mod.sufficiency_check(
        {"retrieved": [], "query": "市盈率是什么，怎么用"}))
    check("概念空检索 -> chat 通用回答", out_concept.get("intent") == "chat", str(out_concept))
    from core.graph import route_after_rag
    check("chat 未命中可路由到 chat", route_after_rag({"intent": "chat"}) == "chat")
    check("stock_agent 未命中仍取数", route_after_rag({"intent": "stock_agent"}) == "stock_agent")
    check("命中走 post", route_after_rag({"intent": "stock"}) == "post")
    check("命中路由到 generate",
          rag_mod.route_after_check({"intent": "stock"}) == "generate")
    check("未命中路由到 escalate",
          rag_mod.route_after_check({"intent": "stock_agent"}) == "escalate")

    retrieved = [
        {"title": "研报A", "content": "内容", "similarity": 0.9},
        {"title": "研报B", "content": "内容", "similarity": 0.8},
    ]
    orig = rag_mod.get_small_llm
    # 判官缺席（模型异常）-> 按命中处理，退回纯阈值口径
    rag_mod.get_small_llm = lambda **kw: (_ for _ in ()).throw(RuntimeError("no llm"))
    try:
        out2 = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": retrieved, "query": "茅台研报观点"}))
        check("判官异常时按命中（不 escalate）", "intent" not in out2, str(out2))
        check("判官不可用时返回 True", asyncio.run(rag_mod._judge_can_answer("q", retrieved)) is True)
    finally:
        rag_mod.get_small_llm = orig

    ret = [
        {"title": "研报A", "content": "内容", "similarity": 0.9, "rerank_score": 0.99,
         "stock_codes": ["300750"]},
        {"title": "研报B", "content": "内容", "similarity": 0.8, "rerank_score": 0.95,
         "stock_codes": ["300750"]},
    ]

    # 实体不一致：问题点名茅台，但片段全是宁德时代 -> 按未命中处理（不硬答别家）
    async def _maotai(_q):
        return {"600519"}

    rag_mod.entity_codes = _maotai
    rag_mod.get_small_llm = lambda **kw: (_ for _ in ()).throw(RuntimeError("不应判读"))
    try:
        out_mismatch = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": ret, "query": "贵州茅台最新研报观点"}))
        check("实体不一致按未命中（升级取数）", out_mismatch.get("intent") == "stock_agent",
              str(out_mismatch))
    finally:
        rag_mod.get_small_llm = orig

    # 实体一致：片段就是问题这家 -> 高分短路命中
    async def _catl(_q):
        return {"300750"}

    rag_mod.entity_codes = _catl
    rag_mod._judge_runnable = None
    rag_mod.get_small_llm = lambda **kw: (_ for _ in ()).throw(RuntimeError("短路不应判读"))
    try:
        out_hit = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": ret, "query": "宁德时代最新研报观点"}))
        check("实体一致且高分 -> 命中", "intent" not in out_hit, str(out_hit))
    finally:
        rag_mod.entity_codes = orig_ent
        rag_mod.get_small_llm = orig
        rag_mod._judge_runnable = None

    # 判官判「不能回答」-> escalate 到炒股 agent
    class _Verdict:
        can_answer = False
        reason = "资料不含实时价格"

    class _Chain:
        async def ainvoke(self, msgs):
            return _Verdict()

    class _SmallLLM:
        def with_structured_output(self, schema):
            return _Chain()

    rag_mod.entity_codes = _no_entity  # 该用例只验判官，不涉及实体校验
    rag_mod.get_small_llm = lambda **kw: _SmallLLM()
    try:
        out3 = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": retrieved, "query": "茅台现在多少钱"}))
        check("判官判不能 -> stock_agent", out3.get("intent") == "stock_agent", str(out3))
    finally:
        rag_mod.get_small_llm = orig
        rag_mod.entity_codes = orig_ent


def test_intent_taxonomy():
    print("\n[P5] 意图三分与漏斗口径")
    import asyncio

    from core.graph import route_by_intent
    from nodes.classify import _hit_data_keyword, classify

    check("数据关键词命中", _hit_data_keyword("贵州茅台最新行情"))
    check("研报类不算数据关键词", not _hit_data_keyword("帮我读一下这篇研报的观点"))
    st = asyncio.run(classify({"query": "查询SH600519的实时报价", "rewritten": "", "messages": []}))
    check("实时数据问句直连 stock_agent", st.get("intent") == "stock_agent", str(st))
    check("快路径置信度为 1", st.get("confidence") == 1.0, str(st))
    check("stock_agent 能通过主图路由",
          route_by_intent({"intent": "stock_agent"}) == "stock_agent")

    # 文档型问题仍走 RAG：财报/研报/公告可能命中原文，不能为了省检索直接取数
    st_doc = asyncio.run(classify({
        "query": "看一下宁德时代最新的财报主要财务指标", "rewritten": "", "messages": [],
    }))
    check("文档型问句仍判 stock（走 RAG）", st_doc.get("intent") == "stock", str(st_doc))

    # 概念型问题即使含「市盈率/龙虎榜」也留在 RAG
    st_concept = asyncio.run(classify({
        "query": "解释一下市盈率的含义", "rewritten": "", "messages": [],
    }))
    check("概念型问句仍判 stock（走 RAG）", st_concept.get("intent") == "stock", str(st_concept))

    from core.keywords import is_realtime_data_question
    check("行情问句属于实时数据", is_realtime_data_question("茅台现在多少钱"))
    check("年报内容问句不算实时数据",
          not is_realtime_data_question("茅台2024年年报说了什么"))
    check("财报指标问句不算实时数据",
          not is_realtime_data_question("看一下宁德时代最新的财报主要财务指标"))


def test_direct_tools():
    print("\n[P7] 进程内工具集（无 MCP 通道）")
    import asyncio

    from subgraphs import stock as stock_mod
    from tools import get_tools

    # 不写死数量：新增取数工具不该每次都改这条断言
    names = [t.name for t in get_tools()]
    check("工具标签表齐全", all(n in _TOOL_LABELS for n in names), str(names))
    check("知识库检索工具可构造", search_knowledge_base.name == "search_knowledge_base")

    stock_mod._TOOLS = None
    asyncio.run(stock_mod._ensure_tools())
    names2 = [t.name for t in stock_mod._tools()]
    check("子图工具集含知识库检索", "search_knowledge_base" in names2, str(names2))


# ===== P8：benchmark 调优（2026-10-24）新增行为的回归 =====
def test_rewrite_into_toolcall():
    print("\n[P8] 指代消解结果贯通 tool_call（多轮第二轮必须能取数）")
    from subgraphs.stock import _apply_rewrite

    q = "它的市盈率是多少？"
    rewritten = "贵州茅台的市盈率是多少？"
    state = {"query": q, "rewritten": rewritten,
             "messages": [HumanMessage(content=q)]}
    out = _apply_rewrite(state["messages"], state)
    check("本轮问题被替换为消解后的问题", out[0].content == rewritten, str(out[0].content))

    # 单轮完整问题（rewritten == query）不动正文
    state2 = {"query": "贵州茅台行情", "rewritten": "贵州茅台行情",
              "messages": [HumanMessage(content="贵州茅台行情")]}
    out2 = _apply_rewrite(state2["messages"], state2)
    check("完整问题不改写", out2[0].content == "贵州茅台行情")

    # rewritten 为空时不动
    state3 = {"query": q, "rewritten": "", "messages": [HumanMessage(content=q)]}
    check("无消解结果时不动", _apply_rewrite(state3["messages"], state3)[0].content == q)

    # 后续消息（如 ToolMessage）不受影响
    state4 = {"query": q, "rewritten": rewritten, "messages": [
        HumanMessage(content=q),
        AIMessage(content="", tool_calls=[{"name": "get_valuation", "args": {}, "id": "1"}]),
        ToolMessage(content="{}", tool_call_id="1", name="get_valuation"),
    ]}
    out4 = _apply_rewrite(state4["messages"], state4)
    check("后续消息原样保留", len(out4) == 3 and out4[2].tool_call_id == "1")


def test_vague_reference_guard():
    print("\n[P8] 指代不明问句确定性判 clarify")
    import asyncio

    from nodes.classify import _is_vague_reference, classify

    check("「那个股票怎么样」判含糊", _is_vague_reference("那个股票怎么样"))
    check("「这只股票如何」判含糊", _is_vague_reference("这只股票如何"))
    check("带代码的不含糊", not _is_vague_reference("600519这只股票怎么样"))
    check("长句不误伤", not _is_vague_reference("帮我分析一下贵州茅台和五粮液这两只股票哪个更值得长期持有"))
    check("普通问句不误伤", not _is_vague_reference("贵州茅台最新行情"))

    out = asyncio.run(classify({"query": "那个股票怎么样", "rewritten": "",
                                "messages": []}))
    check("含糊问句路由为 clarify", out.get("intent") == "clarify", str(out))


def test_graceful_empty_fallback():
    print("\n[P8] 兜底文案区分「客观无数据」与「取数失败」")
    from subgraphs.stock import _all_failures_graceful_empty, _no_data_fallback

    # 未上榜：客观结论，不应套「请稍后重试」
    tr_lhb = _tool_results("get_top_list", False, "近期未上榜或暂无龙虎榜数据")
    st = {"query": "查一下三花智控最近的龙虎榜席位明细", "rewritten": ""}
    out = _no_data_fallback(st, tr_lhb)
    check("未上榜走客观结论文案", "并非系统故障" in out and "稍后重试" not in out, out)
    check("未上榜判定为客观无数据", _all_failures_graceful_empty(tr_lhb))

    # 连接失败：仍走重试文案
    tr_conn = _tool_results("get_money_flow", False, "ConnectionError: RemoteDisconnected")
    out2 = _no_data_fallback(st, tr_conn)
    check("连接失败仍提示重试", "稍后重试" in out2, out2)
    check("连接失败不是客观无数据", not _all_failures_graceful_empty(tr_conn))

    # 混合失败（一条未上榜 + 一条连接失败）按取数失败处理，不轻言「无数据」
    check("混合失败不判客观无数据",
          not _all_failures_graceful_empty(tr_lhb + tr_conn))


def test_fewshot_examples_wired():
    print("\n[P8] few-shot 示例接入三个提示词")
    from core.template import load_template, render

    for name in ("stock_examples.xml", "router_examples.xml", "toolcall_examples.xml"):
        text = load_template(name)
        check(f"{name} 非空且含示例标记", "<example" in text or "示范" in text, name)

    stock_prompt = render("stock.xml", now="2026-10-24", query="q",
                          tool_results="[]", examples=load_template("stock_examples.xml"))
    check("stock.xml 注入示例", "示范回答的共性" in stock_prompt)
    check("stock.xml 新规则在位（点明标的）", "点明标的" in stock_prompt)

    router_prompt = render("router.xml", now="2026-10-24", history="", query="q",
                           examples=load_template("router_examples.xml"))
    check("router.xml 注入示例", "边界判别示范" in router_prompt)

    toolcall_prompt = render("stock_toolcall.xml",
                             examples=load_template("toolcall_examples.xml"))
    check("stock_toolcall.xml 注入示例", "取数链路示范" in toolcall_prompt)
    check("toolcall 新规则在位（概念检索无果用通识）", "基于通用知识直接作答" in toolcall_prompt)


def test_answer_format_rules():
    print("\n[P17] 回答版式规范（分段 / 标点 / 不用加粗）")
    from core.template import load_template, render

    stock_prompt = render("stock.xml", now="2026-10-24", query="q",
                          tool_results="[]", examples=load_template("stock_examples.xml"))
    check("stock.xml 分段规则在位", "段落结构" in stock_prompt)
    check("stock.xml 标点规则在位", "标点规范" in stock_prompt)
    check("stock.xml 禁用加粗", "不用加粗（**）" in stock_prompt)
    check("stock.xml 分点限 1-2 句", "每条只讲一件事，控制在 1-2 句" in stock_prompt)

    chat_prompt = render("chat.xml", now="2026-10-24", query="q")
    check("chat.xml 版式规则在位", "版式：" in chat_prompt)
    check("chat.xml 禁用加粗", "不用标题（#）、分隔线、加粗（**）" in chat_prompt)

    examples = load_template("stock_examples.xml")
    check("示例不再示范加粗", "**市盈率" not in examples)
    check("示例声明每条分点 1-2 句", "每条分点 1-2 句" in examples)


# ===== P9：会话历史与 checkpoint 增长治理（2026-10-24）=====
def test_history_trim_and_session_utils():
    print("\n[P9] 消息窗口裁剪与会话注册表工具")
    from core.sessions import thread_id_of, valid_session_id
    from nodes.guardrail import _trim_history

    # 窗口内不裁
    msgs = [HumanMessage(content=f"q{i}") for i in range(10)]
    check("窗口内原样返回", _trim_history(msgs) == msgs)

    # 超窗口只保留末尾，且最后一条是本轮新问题
    msgs = [HumanMessage(content=f"q{i}") for i in range(80)]
    out = _trim_history(msgs)
    check("裁到窗口大小", len(out) == 40, str(len(out)))
    check("末尾是本轮问题", out[-1].content == "q79")

    # 头部悬挂 ToolMessage 被丢弃（其配对的 AIMessage 已被裁掉，
    # 孤儿 ToolMessage 会让上游接口报错）
    msgs = ([ToolMessage(content="{}", tool_call_id="1", name="get_quote")]
            + [HumanMessage(content=f"q{i}") for i in range(45)])
    out = _trim_history(msgs)
    check("开头孤儿 ToolMessage 被丢", not isinstance(out[0], ToolMessage))
    check("裁剪后以人类消息开头", isinstance(out[0], HumanMessage))

    # 成对的 tool_calls + ToolMessage 整体保留（不被拦腰切断）
    msgs = ([HumanMessage(content="old")] * 30
            + [HumanMessage(content="q"),
               AIMessage(content="", tool_calls=[{"name": "get_quote", "args": {}, "id": "1"}]),
               ToolMessage(content="{}", tool_call_id="1", name="get_quote"),
               AIMessage(content="答")])
    out = _trim_history(msgs)
    check("配对的 tool 消息整体保留", any(isinstance(m, ToolMessage) for m in out))

    # thread_id 口径与 Go 网关 sse_agent.go 一致
    check("thread_id 拼装口径", thread_id_of("42", "sess_1") == "default:42:sess_1")
    check("合法 session_id 通过", valid_session_id("sess_123_abc"))
    check("非法 session_id 被拒",
          not valid_session_id("a b") and not valid_session_id("x" * 65) and not valid_session_id(""))


# ===== P10：检索候选池与上线数量（2026-10-25）=====
def test_retrieval_knobs():
    print("\n[P10] 候选池 N / 上线数量 k 的接线与裁剪")
    from langchain_core.documents import Document

    from config import RAG_PER_DOC, RERANK_CANDIDATES
    from rag.retriever import (
        _apply_rerank_filter,
        _build_search_sql,
        _clean_docs,
        _normalize_since,
        _normalize_source_type,
        _normalize_stock_code,
        build_retriever,
    )

    r = build_retriever(k=4, candidates=30)
    check("k 与 candidates 可分别配置", r.k == 4 and r.candidates == 30, f"{r.k}/{r.candidates}")
    check("candidates 默认取 RERANK_CANDIDATES",
          build_retriever(k=6).candidates == RERANK_CANDIDATES,
          f"{build_retriever(k=6).candidates} vs {RERANK_CANDIDATES}")

    def doc(i, title, rs, head=None):
        return Document(page_content=(head or i) + "x" * 60, metadata={
            "id": i, "title": title, "similarity": 0.8, "rerank_score": rs})

    docs = [doc("a", "t1", 0.9), doc("b", "t1", 0.8), doc("c", "t1", 0.7), doc("d", "t2", 0.1)]
    check("裁剪关闭时原样返回", len(_apply_rerank_filter(docs, 0.5, enabled=False)) == 4)
    check("裁剪开启时按阈值过滤",
          [x.metadata["id"] for x in _apply_rerank_filter(docs, 0.5, enabled=True)] == ["a", "b", "c"])
    # 重排分缺失（重排关闭/失败）时不能把候选裁空，否则误判为「检索不足」
    nod = [Document(page_content="z" * 60, metadata={"id": "e", "title": "t3", "similarity": 0.8})]
    check("无重排分时不裁空", len(_apply_rerank_filter(nod, 0.5, enabled=True)) == 1)
    check("per_doc 限制同文档条数（无 doc_id 时回落标题）",
          [x.metadata["id"] for x in _clean_docs(docs, per_doc=2)] == ["a", "b", "d"])
    check("per_doc 默认取 RAG_PER_DOC", build_retriever(k=6).per_doc == RAG_PER_DOC)

    # 同标题不同 doc_id（年报标题在库里高度重复）：按 doc_id 限流，不能互相挤掉
    def ddoc(i, doc_id, title="2025年年度报告"):
        return Document(page_content=i + "y" * 60, metadata={
            "id": i, "doc_id": doc_id, "title": title, "similarity": 0.8, "rerank_score": 0.9})

    multi = [ddoc("m1", "docA"), ddoc("m2", "docA"), ddoc("m3", "docA"), ddoc("m4", "docB")]
    kept_doc = [x.metadata["id"] for x in _clean_docs(multi, per_doc=2)]
    check("同标题不同文档各自计数", kept_doc == ["m1", "m2", "m4"], str(kept_doc))

    # 过滤下推到 vec/lex 两路；非法值被忽略，不能把 SQL 拼坏
    plain_sql, plain_params = _build_search_sql()
    # 外层始终左连语料表拿 stock_codes（命中闸的实体校验用），但无过滤时不应产生过滤参数
    check("无过滤时不产生过滤参数", not plain_params, str(plain_params))
    check("外层左连语料表取 stock_codes",
          "LEFT JOIN ashare_documents a ON a.doc_id=c.document_id" in plain_sql
          and "a.stock_codes" in plain_sql, plain_sql[:120])
    check("中文全文检索改用 tsv_zh + to_tsquery",
          "c.tsv_zh @@ q" in plain_sql and "to_tsquery" in plain_sql and "websearch_to_tsquery" not in plain_sql,
          plain_sql[:120])
    check("过滤值归一化", (
        _normalize_stock_code("sh600519") == "600519"
        and _normalize_source_type("研报") == "research_report"
        and _normalize_source_type("不存在") == ""
        and _normalize_since("2025-1-1") == ""
        and _normalize_since("2025-01-01") == "2025-01-01 00:00:00+08:00"
    ))
    filt_sql, filt_params = _build_search_sql("600519", "research_report", "2025-01-01 00:00:00+08:00")
    check("过滤条件覆盖两路召回",
          # 外层 SELECT 也有一处 a.stock_codes（命中闸实体校验），过滤条件本身仍在两路 CTE
          filt_sql.count("a.stock_codes") == 3 and filt_sql.count("a.source_type") == 2
          and filt_sql.count("a.pub_time") == 2, filt_sql)
    check("过滤参数只传一份", set(filt_params) == {"code", "stype", "since"}, str(filt_params))

    # 去重键必须用归一化全文。ingest 给每个 chunk 注入「[标题路径]」前缀，
    # 同标题兄弟片段前 40 字完全一致，按前缀比对会把不同段落整条误删
    # （等价于把 per_doc 压成 1）——下面两组前 40 字刻意相同。
    prefix = "[投教手册 > 估值指标 > 市盈率的适用边界] " + "零" * 40

    def seg(i, body):
        return Document(page_content=prefix + body, metadata={
            "id": i, "title": "t1", "similarity": 0.8, "rerank_score": 0.9})

    sib = [seg("f", "甲段正文内容"), seg("g", "乙段正文内容")]
    kept = [x.metadata["id"] for x in _clean_docs(sib, per_doc=2)]
    check("同标题同前缀不同正文都保留", kept == ["f", "g"], str(kept))
    dup = [seg("h", "同一段正文"), seg("i", "同一段正文")]
    check("不同 id 的同一段正文被去重", len(_clean_docs(dup, per_doc=2)) == 1)
    ws = [seg("k", "同一段 正文"), seg("l", "同一段\n\n正文")]
    check("仅空白长度差异也判为同一段", len(_clean_docs(ws, per_doc=2)) == 1)
    same_id = [seg("j", "甲"), seg("j", "乙")]
    check("同 id 重复被去重", len(_clean_docs(same_id, per_doc=2)) == 1)


def test_rag_judge_short_circuit():
    print("\n[P15] RAG 高分短路（跳过一次 judge）")
    import asyncio

    from subgraphs import rag as rag_mod

    orig = rag_mod.get_small_llm
    orig_ent = rag_mod.entity_codes

    async def _no_entity(_q):
        return set()

    rag_mod.entity_codes = _no_entity
    rag_mod._judge_runnable = None
    rag_mod.get_small_llm = lambda **kw: (_ for _ in ()).throw(RuntimeError("judge 不应被调用"))
    try:
        hi = [
            {"title": "研报A", "content": "内容", "similarity": 0.9, "rerank_score": 0.95},
            {"title": "研报B", "content": "内容", "similarity": 0.8, "rerank_score": 0.9},
        ]
        out = asyncio.run(rag_mod.sufficiency_check({"retrieved": hi, "query": "茅台研报观点"}))
        check("高分短路不调 judge", "intent" not in out, str(out))
        check("短路时 judge_ms=0", (out.get("timings") or {}).get("judge_ms") == 0, str(out))
    finally:
        rag_mod.get_small_llm = orig

    class _Verdict:
        can_answer = False
        reason = "资料不含实时价格"

    class _Chain:
        async def ainvoke(self, msgs):
            return _Verdict()

    class _SmallLLM:
        def with_structured_output(self, schema):
            return _Chain()

    rag_mod._judge_runnable = None
    rag_mod.get_small_llm = lambda **kw: _SmallLLM()
    try:
        mid = [
            {"title": "研报A", "content": "内容", "similarity": 0.9, "rerank_score": 0.7},
            {"title": "研报B", "content": "内容", "similarity": 0.8, "rerank_score": 0.6},
        ]
        out2 = asyncio.run(rag_mod.sufficiency_check({"retrieved": mid, "query": "茅台现在多少钱"}))
        check("未达短路阈值仍判读", out2.get("intent") == "stock_agent", str(out2))
    finally:
        rag_mod.get_small_llm = orig
        rag_mod.entity_codes = orig_ent
        rag_mod._judge_runnable = None


def test_rewrite_route_merge():
    print("\n[P15] 多轮改写与意图合并为一次调用")
    import asyncio

    from langchain_core.messages import AIMessage, HumanMessage

    from nodes.classify import classify
    from nodes.rewrite import rewrite as rewrite_node

    rw = rewrite_node.__globals__

    hist = [
        HumanMessage(content="贵州茅台现在多少钱"),
        AIMessage(content="约1500元"),
        HumanMessage(content="那它的市盈率呢"),
    ]
    state = {"query": "那它的市盈率呢", "messages": hist, "thread_id": ""}

    class _Decision:
        rewritten = "贵州茅台的市盈率是多少"
        intent = "stock"
        confidence = 0.95
        reason = ""
        symbol = "贵州茅台"
        market = "SH"
        date_range = ""

    class _Chain:
        async def ainvoke(self, msgs):
            return _Decision()

    class _LLM:
        def with_structured_output(self, schema):
            return _Chain()

    orig = rw["get_small_llm"]
    rw["_merge_runnable"] = None
    rw["get_small_llm"] = lambda **kw: _LLM()
    try:
        out = asyncio.run(rewrite_node(state))
    finally:
        rw["get_small_llm"] = orig
        rw["_merge_runnable"] = None
    check("合并路径产出改写", out.get("rewritten") == "贵州茅台的市盈率是多少", str(out))
    check("合并路径产出意图（关键词兜底直连取数）",
          out.get("intent") == "stock_agent" and out.get("confidence") == 1.0, str(out))
    check("合并路径带实体", (out.get("entities") or {}).get("symbol") == "贵州茅台", str(out))

    # 合并调用异常 -> 回退只改写，不写 intent（交给 classify）
    class _FailLLM:
        def with_structured_output(self, schema):
            raise RuntimeError("structured unavailable")

    rw["_merge_runnable"] = None
    rw["get_small_llm"] = lambda **kw: _FailLLM()
    try:
        out2 = asyncio.run(rewrite_node(state))
    finally:
        rw["get_small_llm"] = orig
        rw["_merge_runnable"] = None
    check("合并失败不写意图", "intent" not in out2, str(out2))
    check("合并失败仍返回 query 兜底", bool(out2.get("rewritten")), str(out2))

    # classify 见到已有 intent 直接透传，不再调 LLM
    orig_cls = classify.__globals__["get_small_llm"]
    classify.__globals__["get_small_llm"] = lambda **kw: (_ for _ in ()).throw(
        RuntimeError("classify 不应重复调用"))
    try:
        out3 = asyncio.run(classify({
            "query": "那它的市盈率呢", "rewritten": "贵州茅台的市盈率是多少",
            "intent": "stock", "confidence": 0.95, "messages": [],
        }))
    finally:
        classify.__globals__["get_small_llm"] = orig_cls
    check("已路由时 classify 透传", "intent" not in out3, str(out3))
    check("透传时 classify_ms=0", (out3.get("timings") or {}).get("classify_ms") == 0, str(out3))

    # 单轮：messages 只含当前问题，不得当作有历史触发改写
    import asyncio as _asyncio
    single = _asyncio.run(rewrite_node({"query": "用通俗的语言解释一下市盈率是什么？",
                                        "messages": [HumanMessage(content="用通俗的语言解释一下市盈率是什么？")],
                                        "thread_id": ""}))
    check("单轮当前问题不算历史", single.get("rewritten") == "用通俗的语言解释一下市盈率是什么？",
          str(single))
    check("单轮不写意图", "intent" not in single, str(single))


def test_kb_sources_event():
    print("\n[P15] 知识库工具 sources 事件")
    import asyncio

    from langchain_core.messages import AIMessage, HumanMessage

    from subgraphs import stock as stock_mod

    class _FakeTool:
        name = "search_knowledge_base"

        async def ainvoke(self, args):
            return {"ok": True, "data": [
                {"title": "研报A", "content": "x", "chunk_id": "11",
                 "score": 0.91, "similarity": 0.7},
                {"title": "研报A", "content": "x", "chunk_id": "11",
                 "score": 0.91, "similarity": 0.7},
            ]}

    captured = []
    orig_tools, orig_emit = stock_mod._TOOLS, stock_mod.emit
    stock_mod._TOOLS = [_FakeTool()]
    stock_mod.emit = lambda ev, data: captured.append((ev, data))
    try:
        ai = AIMessage(content="", tool_calls=[{
            "name": "search_knowledge_base", "args": {"query": "q"}, "id": "1",
        }])
        state = {"messages": [HumanMessage(content="q"), ai], "tool_results": [], "timings": {}}
        out = asyncio.run(stock_mod.tool_execute(state))
    finally:
        stock_mod._TOOLS, stock_mod.emit = orig_tools, orig_emit

    src = [data for ev, data in captured if ev == "sources"]
    check("知识库结果发出 sources", len(src) == 1, str(captured))
    check("sources 按 chunk_id 去重", len(src[0]["docs"]) == 1, str(src))
    check("sources 含 chunk_id", src[0]["docs"][0]["chunk_id"] == "11", str(src))
    check("tool_results 正常累加", len(out.get("tool_results") or []) == 1, str(out))


def test_entity_codes():
    print("\n[P16] 查询实体识别（名称/代码）")
    import asyncio

    from rag import entity

    orig = entity._stock_names

    async def _names():
        return [("贵州茅台", "600519"), ("宁德时代", "300750")]

    entity._stock_names = _names
    try:
        check("全名匹配", asyncio.run(entity.entity_codes("贵州茅台三季报")) == {"600519"})
        check("代码匹配", asyncio.run(entity.entity_codes("600519 最新价")) == {"600519"})
        check("多实体", asyncio.run(entity.entity_codes("贵州茅台和宁德时代对比")) == {"600519", "300750"})
        check("无实体", asyncio.run(entity.entity_codes("市盈率是什么")) == set())
        check("简称不匹配全名（保守不误判）", asyncio.run(entity.entity_codes("茅台三季报")) == set())
    finally:
        entity._stock_names = orig


def test_concept_not_swallowing_live_quote():
    print("\n[P18] 行情问句不被「是什么」误判成概念问题")
    # 线上复现：问「襄阳轴承（000678）现在什么价？今天这波涨跌是什么逻辑」，命中了
    # 概念词「是什么」→ RAG 未命中时被置 intent=chat → 答「我这边没有实时行情接口」
    st = {"query": "襄阳轴承（000678）现在什么价？今天这波涨跌是什么逻辑，关键价位在哪",
          "rewritten": ""}
    check("不是概念问题", not _is_concept_question(st))
    check("快路径判为取数", fast_intent(st["query"]) == "stock_agent",
          repr(fast_intent(st["query"])))
    # 首屏引导问题模板就是「<股票名>现在什么价？」，不能落到路由 LLM 手里
    check("口语「什么价」也走取数", fast_intent("宁德时代现在什么价？") == "stock_agent")
    # 真概念题仍保持概念口径（否则会被迫去取数）
    check("概念题不受影响", _is_concept_question({"query": "市盈率是什么，和市净率有什么区别",
                                              "rewritten": ""}))


def test_stream_cleaner_strips_prompt_leak():
    print("\n[P18] 模型漏出的提示词标签在流式层被剥掉")
    # 按线上真实的增量边界喂（闭合标签被拆成 "</" / "user" / "_query" / ">"）
    deltas = ["<", "user", "_query", ">\n襄阳", "轴承", "</", "user", "_query", ">\n\n",
              "<", "system", "_context", ">\n内部推理", "</", "system", "_context", ">\n\n",
              "<", "assistant", "_response", ">\n抱歉，这轮没取到。", "</", "assistant",
              "_response", ">"]
    c = StreamCleaner()
    out = "".join(c.feed(d) for d in deltas) + c.flush()
    check("问句块被整块丢弃", "襄阳轴承" not in out, repr(out))
    check("推理块被整块丢弃", "内部推理" not in out, repr(out))
    check("答案正文保留", out.strip() == "抱歉，这轮没取到。", repr(out))
    check("没有残留标签", "<" not in out and ">" not in out, repr(out))
    # 正常回答零改动（逐字喂也一样）
    normal = "贵州茅台（SH600519）最新价 1338 元。\n\n- 今开 1325.4 元"
    c2 = StreamCleaner()
    got = "".join(c2.feed(normal[i:i + 2]) for i in range(0, len(normal), 2)) + c2.flush()
    check("正常文本原样透传", got == normal, repr(got[:40]))
    # 正文里的数学尖括号不能被当成标签
    c3 = StreamCleaner()
    got3 = "".join(c3.feed(s) for s in ["若 PE ", "<", " 10 则低估"]) + c3.flush()
    check("普通尖括号保留", got3 == "若 PE < 10 则低估", repr(got3))


def test_event_card_desc_clean():
    print("\n[P18] 公司动态卡不把资讯里的数据表当摘要")
    # 线上实测：正文以「代码 名称 涨幅 金额」表格开头，截 60 字后卡片是一串数字
    table = ("1.67 银行 603626 科森科技 9.95 11245.13 100326.80 6.82 电子 688322 奥比中光"
             " 2.31 8873.41 90211.02 5.12 有色 600362 江西铜业")
    check("表格正文不出摘要", _event_desc(table) == "", repr(_event_desc(table)))
    check("一句话正文取首句",
          _event_desc("公司发布公告，拟回购股份用于股权激励。具体金额以公告为准。")
          == "公司发布公告，拟回购股份用于股权激励", repr(_event_desc("公司发布公告，拟回购股份用于股权激励。具体金额以公告为准。")))
    check("过短正文丢弃", _event_desc("详见公告") == "")
    check("空正文安全", _event_desc(None) == "")


def test_fast_plan_and_chat_shortcut():
    print("\n[P19] 确定性快路径：工具直连与问候跳过路由 LLM")
    check("问候判闲聊", fast_chat("你好") and fast_chat("谢谢！"))
    check("带行情词的问候不算闲聊", not fast_chat("你好，茅台现在什么价"))
    check("长句不算问候", not fast_chat("你好我想了解一下这只股票的估值和资金流向情况"))

    import asyncio

    from core.plan import fast_tool_calls
    from rag import entity

    orig = entity._stock_names

    async def _names():
        return [("宁德时代", "300750"), ("贵州茅台", "600519"), ("比亚迪", "002594")]

    entity._stock_names = _names
    try:
        plan = asyncio.run(fast_tool_calls("600519 现在什么价"))
        check("代码+行情 -> 直连 get_quote",
              plan and plan[0]["name"] == "get_quote" and plan[0]["args"]["code"] == "600519",
              str(plan))

        plan = asyncio.run(fast_tool_calls("贵州茅台现在什么价"))
        check("名称+行情 -> 名称表解析后直连",
              plan and plan[0]["args"]["code"] == "600519", str(plan))

        plan = asyncio.run(fast_tool_calls("宁德时代最近5个交易日K线走势"))
        check("K线 -> get_history_daily 且天数解析正确",
              plan and plan[0]["name"] == "get_history_daily"
              and plan[0]["args"]["days"] == 5, str(plan))

        plan = asyncio.run(fast_tool_calls("宁德时代估值贵不贵"))
        check("估值 -> get_valuation",
              plan and plan[0]["name"] == "get_valuation", str(plan))

        # 名称问句第二跳：复用上一轮 symbol_search 的代码，不再跑一次 LLM 决策
        plan = asyncio.run(fast_tool_calls(
            "宁德时代最近资金流向", [{"tool": "symbol_search",
                                 "result": {"ok": True, "data": [{"code": "300750"}]}}]))
        check("复用 symbol_search 结果取数",
              plan and plan[0]["args"]["code"] == "300750", str(plan))

        check("对比类交还 LLM", asyncio.run(fast_tool_calls("宁德时代和比亚迪哪个好")) is None)
        check("概念题交还 LLM", asyncio.run(fast_tool_calls("市盈率是什么意思")) is None)
        check("标的解析不出时交还 LLM",
              asyncio.run(fast_tool_calls("这个板块现在什么价")) is None)
    finally:
        entity._stock_names = orig


def main():
    print("=" * 62)
    print("AI 对话故障回归测试")
    print("=" * 62)
    test_overflow_detection()
    test_fit_context()
    test_recent_history_drops_tool_messages()
    test_take_latest()
    test_valuation_window()
    test_asked_for_data()
    test_failure_reason()
    test_no_data_fallback()
    test_concept_question_not_blocked()
    test_data_question_always_fetches()
    test_graph_wiring_and_retry()
    test_tool_short_circuit()
    test_graph_short_circuit_after_tool_success()
    test_stage_timings_and_ttft()
    test_llm_retry_config()
    test_text_tool_call_restore()
    test_card_validation()
    test_card_selection()
    test_no_disclaimer()
    test_off_topic_refusal()
    test_rag_judge_fallback()
    test_intent_taxonomy()
    test_direct_tools()
    test_rewrite_into_toolcall()
    test_vague_reference_guard()
    test_graceful_empty_fallback()
    test_fewshot_examples_wired()
    test_answer_format_rules()
    test_history_trim_and_session_utils()
    test_retrieval_knobs()
    test_zh_tokenize()
    test_rag_judge_short_circuit()
    test_rewrite_route_merge()
    test_kb_sources_event()
    test_entity_codes()
    test_concept_not_swallowing_live_quote()
    test_stream_cleaner_strips_prompt_leak()
    test_event_card_desc_clean()
    test_fast_plan_and_chat_shortcut()
    print("\n" + "=" * 62)
    if _FAILED:
        print(f"FAILED: {len(_FAILED)} 项 -> {_FAILED}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
