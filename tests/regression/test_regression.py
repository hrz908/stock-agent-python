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
2026-10-27 新增：
  P20 回复去重（重复段落 / 重复工具结果 / 同类卡片）与卡片改到正文之后

2026-10-28 新增：
  P21 池外语料覆盖闸（点名标的没有语料时出固定话术，不升级取数硬答）

2026-10-29 新增：
  P22 覆盖闸降级（问当下状态的池外标的降级取数，问原文 / 无工具口径的仍出话术）
  P23 supervisor 合并改写 + 路由（单轮一次调用、结构化上下文注入、快路径短路）
  P25 回答不带过程说明（提示词禁令 + 流式层整行丢弃知识库超时 / 数据缺口等交代）

用法：python tests/regression/test_regression.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.config import MAX_INPUT_TOKENS
from agent.core.keywords import fast_chat, fast_intent
from agent.core.llm import count_message_tokens, is_context_overflow
from agent.core.sanitize import StreamCleaner
from agent.nodes.refuse import _REFUSE_TEXT, refuse
from agent.subgraphs.cards import validate_card, validate_table

# stock 子图已按职责拆分（_cards / _fallback / _state / _toolcall），
# 这里按真实归属导入；stock_mod 指向节点所在的 _toolcall 模块（测试要打补丁）。
from agent.subgraphs.stock import _cards as stock_cards
from agent.subgraphs.stock import _state as stock_state
from agent.subgraphs.stock import _toolcall as stock_mod
from agent.subgraphs.stock._cards import (
    _card_candidates,
    _dedupe_tool_results,
    _event_desc,
)
from agent.subgraphs.stock._fallback import (
    _DATA_ABBR_RE,
    _asked_for_data,
    _failure_reason,
    _got_data,
    _is_concept_question,
    _needs_data_fetch,
    _no_data_fallback,
)
from agent.subgraphs.stock._fallback import (
    OVERFLOW_REPLY as _OVERFLOW_REPLY,
)
from agent.subgraphs.stock._state import _TOOL_LABELS
from agent.subgraphs.stock._toolcall import (
    _call_rounds,
    _fit_context,
    _normalize_tool_call,
    _parse_text_tool_call,
    _pending_nudge,
    _recent_history,
    route_after_call,
    route_after_execute,
)
from agent.tools import take_latest, tool_cache_clear
from agent.tools.rag_tool import search_knowledge_base

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
    from agent.tools.fundamental import _MAX_VALUATION_ROWS, _date_window
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
    check("兜底只陈述没有", "没有" in out, out)
    check("兜底不交代失败原因", "频率" not in out and "超时" not in out, out)
    check("兜底不建议重试", "稍后" not in out and "重试" not in out, out)
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
    orig_tool_bound = stock_mod._tool_bound_llm

    async def _no_plan(state):
        # 本用例只验证「模型没调工具 -> 有界重试」的接线，不能被确定性规划接管
        return None

    # 决策轮用的是 _state 里懒绑定的模型（拆分后不再共享 _toolcall.get_llm），
    # 两个入口都指到假模型，否则会去打真实 LLM
    stock_mod.get_llm = lambda **kw: fake
    stock_mod._tool_bound_llm = lambda: fake
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
        stock_mod._tool_bound_llm = orig_tool_bound


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
    fail = {"tool_results": _tool_results("get_quote", False, "connection reset")}
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
    orig_tools = stock_state._TOOLS
    orig_bound = stock_state._bound_llm
    stock_mod._tool_bound_llm = lambda: tool_llm
    stock_mod.get_llm = lambda **kw: _FakeStreamLLM()
    stock_state._TOOLS = [_FakeTool()]
    stock_state._bound_llm = None
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
        stock_state._TOOLS = orig_tools
        stock_state._bound_llm = orig_bound


def test_stage_timings_and_ttft():
    print("\n[P13] 阶段计时与 TTFT 口径")
    import asyncio

    from agent.core.events import _is_answer_event
    from agent.core.state import initial_state, merge_timings
    from agent.nodes.classify import classify
    from agent.nodes.guardrail import guardrail
    from agent.nodes.rewrite import rewrite

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
    from shared.zh_tokenize import document_text, query_tsquery

    doc = document_text("贵州茅台发布了回购公告，投资者可关注")
    check("中文按 bigram 切分", "贵州" in doc and "州茅" in doc and "茅台" in doc, doc)
    check("ASCII 代码保留", "600519" in document_text("查询600519最新价"), document_text("查询600519最新价"))
    check("重复 bigram 保留（供 tf 计分）", document_text("茅台茅台") == "茅台 台茅 茅台",
          document_text("茅台茅台"))
    q = query_tsquery("贵州茅台 回购 600519")
    check("查询词元去重 OR 拼接", "茅台 | 回购 | 600519" in q, q)
    check("空文本哨兵", query_tsquery("") == "__empty__")
    check("单字虚词过滤", query_tsquery("的") == "__empty__", query_tsquery("的"))
    # 单一来源：检索侧与入库侧必须指向同一份实现，否则 query/document 词元口径分叉，
    # pg 全文检索会静默退化成「永远查不到」（早先 agent 与 ingest 各存一份拷贝）
    from agent.rag import retriever

    check("检索侧复用 shared 实现", retriever.query_tsquery is query_tsquery)
    from ingest import store as ingest_store

    check("入库侧复用 shared 实现", ingest_store.document_text is document_text)


def test_llm_retry_config():
    print("\n[P13] LLM 超时与重试收口")
    from agent.config import LLM_MAX_RETRIES, SMALL_LLM_TIMEOUT, TOOL_CALL_LLM_TIMEOUT
    from agent.core import llm as llm_mod

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

    emitted = []
    orig_card, orig_table = stock_cards.emit_card, stock_cards.emit_table
    stock_cards.emit_card = lambda t, p: emitted.append(t)
    stock_cards.emit_table = lambda t, p: emitted.append(t)

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
        stock_cards._emit_structured({"query": query, "rewritten": ""}, tr)
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
        # 2026-10-27 实测漏判：口语说「财务表现 / 赚钱能力」不提「财报」，
        # 词表没覆盖就按工具顺序落到估值卡上去了
        check("财务表现问句只发财报卡", run("茅台最近财务表现如何",
                                    [valuation, report]) == ["report_card"])
        check("赚钱能力问句只发财报卡", run("茅台这几年的赚钱能力怎么样",
                                     [valuation, report]) == ["report_card"])
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
        stock_cards.emit_card, stock_cards.emit_table = orig_card, orig_table


def test_kline_card_title_carries_symbol():
    """只调 K 线工具（问「走势 / 量能」的常见路径）时，卡片标题也要能带上标的名。

    标题缺名会一直停在兜底的「K 线走势」，看不出是哪只票。
    """
    print("\n[P11b] K 线卡标题带标的名")

    rows = [{"trade_date": "2026-10-23", "open": 1, "high": 2,
             "low": 0.5, "close": 1.5, "vol": 100}]
    tr = [{"tool": "get_history_daily", "result": {"ok": True, "data": rows}}]

    with_symbol = stock_cards._card_candidates(tr, {"entities": {"symbol": "金安国纪"}})
    check("没查行情也能带标的名",
          bool(with_symbol) and with_symbol[0][1].get("name") == "金安国纪")

    without = stock_cards._card_candidates(tr, {})
    check("取不到标的时不硬编名字", bool(without) and "name" not in without[0][1])


# ===== P12：正文不再追加免责声明（2026-10-26）=====
def test_no_disclaimer():
    print("\n[P12] 收尾节点不改写正文、不追加免责声明")
    import asyncio

    from agent.nodes.post import post

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
    check("话术含身份与引导", "溜来" in msg.content and "股票" in msg.content)
    check("婉拒不反问", "？" not in msg.content and "?" not in msg.content)


def test_rag_judge_fallback():
    print("\n[P5] RAG 两级命中判读")
    import asyncio

    from agent.subgraphs import rag as rag_mod

    orig_mentions = rag_mod.entity_mentions
    orig_cov = rag_mod.coverage.detail

    async def _no_entity(_q):
        return []

    async def _no_coverage(_codes):
        # 回归测试不连库：覆盖判定返回空 = 不可用，该闸按 fail-open 跳过
        return {}

    rag_mod.entity_mentions = _no_entity
    rag_mod.coverage.detail = _no_coverage
    out = asyncio.run(rag_mod.sufficiency_check({"retrieved": [], "query": "x"}))
    check("空检索 -> stock_agent", out.get("intent") == "stock_agent", str(out))
    out_concept = asyncio.run(rag_mod.sufficiency_check(
        {"retrieved": [], "query": "市盈率是什么，怎么用"}))
    check("概念空检索 -> chat 通用回答", out_concept.get("intent") == "chat", str(out_concept))
    from agent.core.graph import route_after_rag
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
        return ["600519"]

    rag_mod.entity_mentions = _maotai
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
        return ["300750"]

    rag_mod.entity_mentions = _catl
    rag_mod._judge_runnable = None
    rag_mod.get_small_llm = lambda **kw: (_ for _ in ()).throw(RuntimeError("短路不应判读"))
    try:
        out_hit = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": ret, "query": "宁德时代最新研报观点"}))
        check("实体一致且高分 -> 命中", "intent" not in out_hit, str(out_hit))
    finally:
        rag_mod.entity_mentions = orig_mentions
        rag_mod.coverage.detail = orig_cov
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

    rag_mod.entity_mentions = _no_entity  # 该用例只验判官，不涉及实体校验
    rag_mod.get_small_llm = lambda **kw: _SmallLLM()
    try:
        out3 = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": retrieved, "query": "茅台现在多少钱"}))
        check("判官判不能 -> stock_agent", out3.get("intent") == "stock_agent", str(out3))
    finally:
        rag_mod.get_small_llm = orig
        rag_mod.entity_mentions = orig_mentions
        rag_mod.coverage.detail = orig_cov


def test_rag_coverage_gate():
    print("\n[P21] 池外标的的覆盖闸")
    import asyncio

    from agent.subgraphs import rag as rag_mod

    orig_mentions = rag_mod.entity_mentions
    orig_cov = rag_mod.coverage.detail

    async def _mentions(_q):
        return ["600519"]

    def _cov(has_docs: bool, in_pool: bool):
        async def _f(_codes):
            return {"600519": (has_docs, in_pool)}

        return _f

    try:
        rag_mod.entity_mentions = _mentions

        rag_mod.coverage.detail = _cov(False, False)
        out = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": [], "query": "贵州茅台业务构成"}))
        check("池外标的 -> out_of_scope", out.get("corpus_miss_reason") == "out_of_scope", str(out))
        check("池外标的 -> 不升级取数", "intent" not in out, str(out))
        check("覆盖闸路由到 not_covered",
              rag_mod.route_after_check(out) == "not_covered", str(out))
        text = rag_mod._NOT_COVERED_TEXT["out_of_scope"].format(subject="600519", unit="这只标的")
        check("话术说明采集范围", "300" in text and "语料" in text)

        rag_mod.coverage.detail = _cov(False, True)
        out2 = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": [], "query": "贵州茅台业务构成"}))
        check("在池未采集 -> pending", out2.get("corpus_miss_reason") == "pending", str(out2))

        rag_mod.coverage.detail = _cov(True, True)
        out3 = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": [], "query": "贵州茅台业务构成"}))
        check("有语料 -> 走常规判定", "corpus_miss" not in out3, str(out3))

        # 数据类问句另有实时工具，不能被覆盖闸挡掉
        rag_mod.coverage.detail = _cov(False, False)
        out4 = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": [], "query": "贵州茅台最新市盈率"}))
        check("数据类问句不被拦截", "corpus_miss" not in out4, str(out4))

        async def _boom(_codes):
            raise RuntimeError("db down")

        rag_mod.coverage.detail = _boom
        out5 = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": [], "query": "贵州茅台业务构成"}))
        check("判定异常 fail-open", "corpus_miss" not in out5, str(out5))
    finally:
        rag_mod.entity_mentions = orig_mentions
        rag_mod.coverage.detail = orig_cov


def test_rag_coverage_downgrade():
    print("\n[P22] 覆盖闸降级：问当下状态走取数，问原文仍出话术")
    import asyncio

    from agent.subgraphs import rag as rag_mod

    orig_mentions = rag_mod.entity_mentions
    orig_cov = rag_mod.coverage.detail

    async def _mentions(_q):
        return ["920025"]

    def _cov(has_docs: bool, in_pool: bool):
        async def _f(_codes):
            return {"920025": (has_docs, in_pool)}

        return _f

    try:
        rag_mod.entity_mentions = _mentions
        rag_mod.coverage.detail = _cov(False, False)

        # 问「现在怎么样」：行情 / 资金 / 快讯还能答，降级取数而不是甩话术
        out = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": [], "query": "920025 现在怎么样"}))
        check("问当下状态 -> 降级取数", out.get("intent") == "stock_agent", str(out))
        check("降级仍带 corpus_miss", out.get("corpus_miss") == ["920025"], str(out))
        check("降级路由到 escalate", rag_mod.route_after_check(out) == "escalate", str(out))

        # 问原文：取数工具没有这个口径，降级只会白跑一轮 tool_call
        out2 = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": [], "query": "920025 的年报讲了什么"}))
        check("问原文 -> 不降级", "intent" not in out2, str(out2))
        check("问原文 -> 出话术", rag_mod.route_after_check(out2) == "not_covered", str(out2))

        # 没有工具口径的内容（业务构成 / 竞争格局）：同样不降级
        out3 = asyncio.run(rag_mod.sufficiency_check(
            {"retrieved": [], "query": "920025 的业务构成"}))
        check("无工具口径 -> 不降级", "intent" not in out3, str(out3))
    finally:
        rag_mod.entity_mentions = orig_mentions
        rag_mod.coverage.detail = orig_cov


def test_supervisor_merge():
    print("\n[P23] supervisor：一次调用完成改写 + 路由（含上下文补全）")
    import asyncio
    import importlib

    # 走 importlib 取模块：agent.nodes.__init__ 把同名函数导出到包属性上，
    # `from agent.nodes import supervisor` 拿到的是函数而不是模块
    post_mod = importlib.import_module("agent.nodes.post")
    sup = importlib.import_module("agent.nodes.supervisor")

    class _Decision:
        rewritten = "贵州茅台现在什么价"
        intent = "stock_agent"
        confidence = 0.9
        reason = ""
        symbol = "贵州茅台"
        market = "SH"
        date_range = ""

    class _Runnable:
        def __init__(self):
            self.calls = 0
            self.prompts: list[str] = []
            self.boom = False

        async def ainvoke(self, msgs):
            self.calls += 1
            self.prompts.append(str(msgs[-1].content))
            if self.boom:
                raise RuntimeError("structured output failed")
            return _Decision

    runnable = _Runnable()
    orig_llm = sup.get_small_llm
    orig_runnable = sup._runnable
    orig_ctx = sup.load_context
    orig_summary = sup._get_summary
    sup._runnable = runnable
    sup.get_small_llm = lambda **_kw: runnable

    async def _empty_summary(_tid):
        return ""

    sup._get_summary = _empty_summary

    try:
        # 1) 单轮、无历史：一次调用同时产出改写与意图（原链路要 rewrite + classify 两次）
        #    用不含数据词的追问，确保不会走关键词快路径
        out = asyncio.run(sup.supervisor({"query": "它后续怎么看", "messages": []}))
        check("单轮也走一次合并调用", runnable.calls == 1, str(runnable.calls))
        check("产出改写结果", out.get("rewritten") == "贵州茅台现在什么价", str(out))
        check("产出意图", out.get("intent") == "stock_agent", str(out))
        check("计时记在 supervisor_ms",
              "supervisor_ms" in (out.get("timings") or {}), str(out.get("timings")))

        # 2) 上一轮结构化上下文：拼进 prompt，卡片轮次之后的「它」也能接上
        async def _ctx(_tid):
            return {"symbols": "600519", "rewritten": "贵州茅台现在什么价",
                    "subgraph": "stock_agent"}

        sup.load_context = _ctx
        asyncio.run(sup.supervisor(
            {"query": "这个票后续怎么看", "messages": [], "thread_id": "t1"}))
        check("上下文注入 prompt", "上一轮标的：600519" in runnable.prompts[-1],
              runnable.prompts[-1][-160:])

        # 2.5) 带明确标的的「这个」问句不算含糊：这里的「这个」是指示，不是指代
        sup.load_context = orig_ctx

        async def _mentions(_q):
            return ["300059"]

        orig_sup_ent = sup.entity_mentions
        sup.entity_mentions = _mentions
        try:
            vague = asyncio.run(sup.supervisor(
                {"query": "东方财富这个软件怎么样", "messages": []}))
            check("问句已有明确标的不判澄清", vague.get("intent") != "clarify", str(vague))
        finally:
            sup.entity_mentions = orig_sup_ent

        # 3) 快路径：命中实时数据词不再惊动小模型
        before = runnable.calls
        fast = asyncio.run(sup.supervisor({"query": "SH600519最新行情", "messages": []}))
        check("快路径不调小模型", runnable.calls == before, str(runnable.calls))
        check("快路径判取数", fast.get("intent") == "stock_agent", str(fast))

        # 4) 上游已给意图：透传，不重复调用
        passthrough = asyncio.run(sup.supervisor({"query": "任意", "intent": "stock"}))
        check("已有意图透传", "intent" not in passthrough, str(passthrough))

        # 5) 结构化输出失败：回退 XML 也不可用时按澄清处理，不乱答
        runnable.boom = True
        fb = asyncio.run(sup.supervisor({"query": "茅台业务构成", "messages": []}))
        check("两条路径都失败 -> clarify", fb.get("intent") == "clarify", str(fb))
    finally:
        sup.get_small_llm = orig_llm
        sup._runnable = orig_runnable
        sup.load_context = orig_ctx
        sup._get_summary = orig_summary

    # 6) post 写入结构化上下文：有标的才写
    class _Redis:
        def __init__(self):
            self.data: dict = {}

        async def hset(self, key, mapping=None):
            self.data[key] = mapping or {}

        async def expire(self, key, ttl):
            return None

    fake = _Redis()
    orig_get = post_mod.get_redis
    orig_ent = post_mod.entity_mentions
    post_mod.get_redis = lambda: fake

    async def _entities(_text):
        return ["600519"]

    post_mod.entity_mentions = _entities
    try:
        asyncio.run(post_mod._remember_context({
            "thread_id": "t1", "rewritten": "贵州茅台现在什么价", "intent": "stock_agent"}))
        got = fake.data.get("agent:ctx:t1") or {}
        check("上下文写入标的", got.get("symbols") == "600519", str(got))
        check("上下文写入链路", got.get("subgraph") == "stock_agent", str(got))

        async def _none(_text):
            return []

        post_mod.entity_mentions = _none
        fake.data.clear()
        asyncio.run(post_mod._remember_context(
            {"thread_id": "t2", "query": "随便问问", "messages": []}))
        check("无标的不写上下文", not fake.data, str(fake.data))
    finally:
        post_mod.get_redis = orig_get
        post_mod.entity_mentions = orig_ent


def test_entity_space_name():
    print("\n[P25] 名称归一化与简称识别（五 粮 液 / 茅台 / 招行）")
    import asyncio
    import importlib

    ent = importlib.import_module("agent.rag.entity")

    check("名称归一化去空格", ent._normalize_name("五 粮 液") == "五粮液",
          ent._normalize_name("五 粮 液"))

    rows = [
        ("600519", "贵州茅台"), ("601857", "中国石油"), ("600028", "中国石化"),
        ("600036", "招商银行"), ("300059", "东方财富"), ("000858", "五 粮 液"),
        ("688981", "中芯国际"), ("603758", "隆基机械"), ("601012", "隆基绿能"),
        ("600584", "长电科技"), ("600900", "长江电力"),
    ]
    index = ent.build_name_index(rows)
    names = {name: code for name, code in index}

    check("简称可识别：茅台", names.get("茅台") == "600519", str(names.get("茅台")))
    check("简称可识别：招行 / 东财",
          names.get("招行") == "600036" and names.get("东财") == "300059", str(names))
    check("通用词不做别名（石油 / 石化）", "石油" not in names and "石化" not in names,
          "石油" in names)
    check("别家全名前缀不做别名（隆基 / 长电）",
          "隆基" not in names and "长电" not in names, f"{'隆基' in names}/{'长电' in names}")
    check("唯一前缀仍可做别名（中芯）", names.get("中芯") == "688981", str(names.get("中芯")))
    check("带空格的名称已归一化", names.get("五粮液") == "000858", str(names.get("五粮液")))

    orig = ent._stock_names

    async def _names():
        return index

    ent._stock_names = _names
    ent._mentions_cache.clear()
    try:
        check("带空格名称可识别（用户写紧凑名）",
              asyncio.run(ent.entity_mentions("五粮液现在什么价？")) == ["000858"])
        check("用户照抄带空格的名字也可识别",
              asyncio.run(ent.entity_mentions("五 粮 液现在什么价？")) == ["000858"])
        check("简称走实体识别", asyncio.run(ent.entity_mentions("茅台现在什么价？")) == ["600519"])
        check("歧义名称只认全名", asyncio.run(ent.entity_mentions("长电科技怎么样")) == ["600584"])
    finally:
        ent._stock_names = orig
        ent._mentions_cache.clear()


def test_consult_routing():
    print("\n[P24] 工具/入门咨询前置分流：不进 RAG")
    from agent.core.graph import route_by_intent
    from agent.core.keywords import is_consult_question

    cases = [
        ("我是新手的话 推荐用哪一个股票软件交易", True),
        ("A股开户流程是怎样的", True),
        ("哪家券商佣金低", True),
        ("东财这个软件怎么样", False),          # 问个股，不能被当成工具咨询
        ("推荐几只能买的股票", False),          # 要标的
        ("600519 开户需要多少钱", False),       # 带代码
        ("中国石油现在什么价", False),
    ]
    for text, expect in cases:
        check(f"咨询判定：{text}", is_consult_question(text) is expect, f"expect={expect}")

    check("咨询 -> chat",
          route_by_intent({"intent": "stock",
                           "rewritten": "我是新手的话 推荐用哪一个股票软件交易"}) == "chat")
    check("个股 -> 仍走 RAG",
          route_by_intent({"intent": "stock", "rewritten": "东财这个软件怎么样"}) == "stock")
    check("结构化数据优先 -> 取数",
          route_by_intent({"intent": "stock", "rewritten": "宁德时代主要财务指标"})
          == "stock_agent")


def test_intent_taxonomy():
    print("\n[P5] 意图三分与漏斗口径")
    import asyncio

    from agent.core.graph import route_by_intent
    from agent.nodes.classify import _hit_data_keyword, classify

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

    # 越界强特征：翻译/写代码必须在快路径判 off_topic（实测会被判成 stock_agent）
    from agent.core.keywords import fast_off_topic
    check("翻译合同判越界", fast_off_topic("帮我翻译一段英文合同"))
    check("写代码判越界", fast_off_topic("用 Python 写个快速排序"))
    check("写文档判越界", fast_off_topic("帮我写一封辞职信"))
    check("股票脚本不误伤", not fast_off_topic("用 Python 写个选股脚本"))
    st_off = asyncio.run(classify({
        "query": "用 Python 写个快速排序", "rewritten": "", "messages": [],
    }))
    check("越界问句确定性判 off_topic", st_off.get("intent") == "off_topic", str(st_off))
    st_off2 = asyncio.run(classify({
        "query": "帮我翻译一段英文合同", "rewritten": "", "messages": [],
    }))
    check("翻译请求判 off_topic", st_off2.get("intent") == "off_topic", str(st_off2))

    from agent.core.keywords import is_realtime_data_question
    check("行情问句属于实时数据", is_realtime_data_question("茅台现在多少钱"))
    check("年报内容问句不算实时数据",
          not is_realtime_data_question("茅台2024年年报说了什么"))
    check("财报指标问句不算实时数据",
          not is_realtime_data_question("看一下宁德时代最新的财报主要财务指标"))


def test_direct_tools():
    print("\n[P7] 进程内工具集（无 MCP 通道）")
    import asyncio

    from agent.tools import get_tools

    # 不写死数量：新增取数工具不该每次都改这条断言
    names = [t.name for t in get_tools()]
    check("工具标签表齐全", all(n in _TOOL_LABELS for n in names), str(names))
    check("知识库检索工具可构造", search_knowledge_base.name == "search_knowledge_base")

    stock_state._TOOLS = None
    asyncio.run(stock_state._ensure_tools())
    names2 = [t.name for t in stock_state._tools()]
    check("子图工具集含知识库检索", "search_knowledge_base" in names2, str(names2))


# ===== P8：benchmark 调优（2026-10-24）新增行为的回归 =====
def test_rewrite_into_toolcall():
    print("\n[P8] 指代消解结果贯通 tool_call（多轮第二轮必须能取数）")
    from agent.subgraphs.stock._toolcall import _apply_rewrite

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

    from agent.nodes.classify import _is_vague_reference, classify

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
    from agent.subgraphs.stock._fallback import _all_failures_graceful_empty

    # 未上榜：客观结论直接转述
    tr_lhb = _tool_results("get_top_list", False, "近期未上榜或暂无龙虎榜数据")
    st = {"query": "查一下三花智控最近的龙虎榜席位明细", "rewritten": ""}
    out = _no_data_fallback(st, tr_lhb)
    check("未上榜直接转述结论", "未上榜" in out, out)
    check("未上榜不解释系统状态",
          "系统" not in out and "稍后" not in out and "重试" not in out, out)
    check("未上榜判定为客观无数据", _all_failures_graceful_empty(tr_lhb))

    # 连接失败：只陈述没有，不交代原因也不建议重试
    tr_conn = _tool_results("get_money_flow", False, "ConnectionError: RemoteDisconnected")
    out2 = _no_data_fallback(st, tr_conn)
    check("连接失败只说没有", "没有" in out2, out2)
    check("连接失败不交代原因", "连接" not in out2 and "失败" not in out2, out2)
    check("连接失败不建议重试", "稍后" not in out2 and "重试" not in out2, out2)
    check("连接失败不是客观无数据", not _all_failures_graceful_empty(tr_conn))

    # 混合失败（一条未上榜 + 一条连接失败）按取数失败处理，不轻言「无数据」
    check("混合失败不判客观无数据",
          not _all_failures_graceful_empty(tr_lhb + tr_conn))


def test_fewshot_examples_wired():
    print("\n[P8] few-shot 示例接入三个提示词")
    from agent.core.template import load_template, render

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
    from agent.core.template import load_template, render

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
    from agent.core.sessions import thread_id_of, valid_session_id
    from agent.nodes.guardrail import _trim_history

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

    from agent.config import RAG_PER_DOC, RERANK_CANDIDATES
    from agent.rag.retriever import (
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

    # 软偏好（prefer_codes）：只改变两路召回的排序，不排除其它文档，替代 RAG 漏斗的
    # 「先硬过滤、不足再全库重跑」两趟检索
    pref_sql, pref_params = _build_search_sql(prefer_codes=["600519"])
    check("软偏好不产生 WHERE 过滤（全库仍可兜底）",
          "a.stock_codes && ARRAY" not in pref_sql
          and "CASE WHEN a.stock_codes && %(prefer_codes)s::text[]" in pref_sql,
          pref_sql[:160])
    check("软偏好参数只传一份", set(pref_params) == {"prefer_codes"}, str(pref_params))
    check("软偏好排序前缀覆盖 rk 与两路 ORDER BY（4 处）",
          pref_sql.count("CASE WHEN a.stock_codes && %(prefer_codes)s::text[]") == 4,
          str(pref_sql.count("CASE WHEN")))
    mixed_sql, mixed_params = _build_search_sql("600519", prefer_codes=["000858"])
    check("硬过滤与软偏好同时传入时以硬过滤为准",
          "prefer_codes" not in mixed_params
          and "a.stock_codes && ARRAY[%(code)s]" in mixed_sql, str(mixed_params))

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

    from agent.subgraphs import rag as rag_mod

    orig = rag_mod.get_small_llm
    orig_mentions = rag_mod.entity_mentions
    orig_cov = rag_mod.coverage.detail

    async def _no_entity(_q):
        return []

    async def _no_coverage(_codes):
        return {}

    rag_mod.entity_mentions = _no_entity
    rag_mod.coverage.detail = _no_coverage
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
        rag_mod.entity_mentions = orig_mentions
        rag_mod.coverage.detail = orig_cov
        rag_mod._judge_runnable = None


def test_rewrite_route_merge():
    print("\n[P15] 多轮改写与意图合并为一次调用")
    import asyncio

    from langchain_core.messages import AIMessage, HumanMessage

    from agent.nodes.classify import classify
    from agent.nodes.rewrite import rewrite as rewrite_node

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
    orig_tools, orig_emit = stock_state._TOOLS, stock_mod.emit
    stock_state._TOOLS = [_FakeTool()]
    stock_mod.emit = lambda ev, data: captured.append((ev, data))
    try:
        ai = AIMessage(content="", tool_calls=[{
            "name": "search_knowledge_base", "args": {"query": "q"}, "id": "1",
        }])
        state = {"messages": [HumanMessage(content="q"), ai], "tool_results": [], "timings": {}}
        out = asyncio.run(stock_mod.tool_execute(state))
    finally:
        stock_state._TOOLS, stock_mod.emit = orig_tools, orig_emit

    src = [data for ev, data in captured if ev == "sources"]
    check("知识库结果发出 sources", len(src) == 1, str(captured))
    check("sources 按 chunk_id 去重", len(src[0]["docs"]) == 1, str(src))
    check("sources 含 chunk_id", src[0]["docs"][0]["chunk_id"] == "11", str(src))
    check("tool_results 正常累加", len(out.get("tool_results") or []) == 1, str(out))


def test_entity_codes():
    print("\n[P16] 查询实体识别（名称/代码）")
    import asyncio

    from agent.rag import entity

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
              "<", "assistant", "_response", ">\n贵州茅台最新价 1338 元。", "</", "assistant",
              "_response", ">"]
    c = StreamCleaner()
    out = "".join(c.feed(d) for d in deltas) + c.flush()
    check("问句块被整块丢弃", "襄阳轴承" not in out, repr(out))
    check("推理块被整块丢弃", "内部推理" not in out, repr(out))
    check("答案正文保留", out.strip() == "贵州茅台最新价 1338 元。", repr(out))
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


# ===== P25：回答不带过程说明（2026-10-29）=====
def test_meta_narration_filtered():
    print("\n[P25] 过程说明整行丢弃、正文原样保留")
    # 线上截图 1：知识库超时 + 改用通用知识
    text1 = ("知识库暂时超时不可用，我先按通用常识说明几类工具的差别\n\n"
             "交易软件分两类，账户归属决定你在哪下单。\n"
             "- 券商官方 APP：开在哪家就在哪家下单")
    c = StreamCleaner()
    out = "".join(c.feed(text1[i:i + 3]) for i in range(0, len(text1), 3)) + c.flush()
    check("知识库超时整行丢弃", "知识库" not in out and "通用常识" not in out, repr(out))
    check("正文保留", "交易软件分两类" in out and "券商官方 APP" in out, repr(out))

    # 线上截图 2：本轮工具只返回新闻 + 数据缺口分点
    text2 = ("本轮工具只返回了中国银行的近期新闻，没有返回财务数据，以下仅就消息面提示\n"
             "- 板块位置：四大行总市值首破 10 万亿\n"
             "- 数据缺口：本轮未获取到不良率、净息差等财务数据")
    c2 = StreamCleaner()
    out2 = "".join(c2.feed(text2[i:i + 5]) for i in range(0, len(text2), 5)) + c2.flush()
    check("工具交代整行丢弃", "本轮工具" not in out2 and "以下仅就" not in out2, repr(out2))
    check("数据缺口分点丢弃", "数据缺口" not in out2 and "净息差" not in out2, repr(out2))
    check("有数据的分点保留", "板块位置" in out2, repr(out2))

    # 免责声明 / 资料缺失说明同样按行去掉，正常数据保留
    c3 = StreamCleaner()
    out3 = "".join(c3.feed(s) for s in [
        "以上仅为研报观点梳理，不构成投资建议\n",
        "资料中未提及目标价测算\n",
        "净息差 1.6%，同比收窄 3bp。",
    ]) + c3.flush()
    check("免责声明丢弃", "不构成投资建议" not in out3, repr(out3))
    check("资料缺失说明丢弃", "资料中未提及" not in out3, repr(out3))
    check("正常数据保留", "净息差 1.6%" in out3, repr(out3))

    # 正文里的同一个词不能误杀（用户问的可能就是知识库）
    c4 = StreamCleaner()
    keep = "知识库是企业内部资料的集合，常见于投研检索。"
    got4 = "".join(c4.feed(keep[i:i + 2]) for i in range(0, len(keep), 2)) + c4.flush()
    check("正文中间的知识库不误杀", got4 == keep, repr(got4))

    # 以「知识库检索」开头的正常解释也不能误杀
    c7 = StreamCleaner()
    keep7 = "知识库检索是投研助手常用的补充手段。"
    got7 = "".join(c7.feed(keep7[i:i + 3]) for i in range(0, len(keep7), 3)) + c7.flush()
    check("知识库检索开头的正文不误杀", got7 == keep7, repr(got7))

    # 客观结论不是过程说明，必须保留
    c5 = StreamCleaner()
    got5 = c5.feed("今日未上榜。") + c5.flush()
    check("客观结论不被过滤", got5 == "今日未上榜。", repr(got5))

    # 整条只剩说明时：不输出（宁可不答，也不交代）
    c6 = StreamCleaner()
    got6 = c6.feed("抱歉，这轮没取到数据。") + c6.flush()
    check("整条只剩说明时不输出", got6 == "", repr(got6))


def test_prompt_no_process_narration():
    print("\n[P25] 三个提示词都不交代取数过程")
    from agent.core.template import load_template, render

    stock = render("stock.xml", now="2026-10-29", query="q", tool_results="[]",
                   examples=load_template("stock_examples.xml"))
    check("stock.xml 禁止过程说明", "不出现任何过程说明" in stock)
    check("stock.xml 不再要求交代数据缺失", "数据缺失就如实说明" not in stock)
    check("stock.xml 不再要求标注来源",
          "可引用其定性观点并注明" not in stock and "注明「据通用投资知识」" not in stock)

    chat = render("chat.xml", now="2026-10-29", query="q")
    check("chat.xml 禁止过程说明", "不解释调用 / 检索过程" in chat)

    rag = render("rag.xml", now="2026-10-29", query="q", contexts="（无检索资料）")
    check("rag.xml 不再要求「资料中未提及」", "明确说明「资料中未提及」" not in rag)
    check("rag.xml 禁止来源与免责交代", "不写「据知识库」" in rag)

    ex = load_template("stock_examples.xml")
    check("示例不再示范来源标注", "据通用投资知识" not in ex)
    check("示例示范「没数据的部分不提」", "消息面上需要留意的点" in ex)


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

    from agent.core.plan import fast_tool_calls
    from agent.rag import entity

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

        # 产品约定：走势卡固定 15 根，用户说「5 个交易日」也按 15 取数
        for q in ("宁德时代最近5个交易日K线走势", "宁德时代K线走势", "宁德时代最近30个交易日K线"):
            plan = asyncio.run(fast_tool_calls(q))
            check(f"K线固定取 15 根：{q}",
                  plan and plan[0]["name"] == "get_history_daily"
                  and plan[0]["args"]["days"] == 15, str(plan))

        plan = asyncio.run(fast_tool_calls("宁德时代估值贵不贵"))
        check("估值 -> get_valuation",
              plan and plan[0]["name"] == "get_valuation", str(plan))

        # 名称问句第二跳：复用上一轮 symbol_search 的代码，不再跑一次 LLM 决策
        plan = asyncio.run(fast_tool_calls(
            "宁德时代最近资金流向", [{"tool": "symbol_search",
                                 "result": {"ok": True, "data": [{"code": "300750"}]}}]))
        check("复用 symbol_search 结果取数",
              plan and plan[0]["args"]["code"] == "300750", str(plan))

        # 龙虎榜曾被打进资金流族：问榜单却调 get_money_flow，拿资金流数据冒充席位
        plan = asyncio.run(fast_tool_calls("比亚迪最近的龙虎榜席位明细"))
        check("龙虎榜 -> get_top_list",
              plan and plan[0]["name"] == "get_top_list", str(plan))

        # 量能对比要同时取快照与历史序列
        plan = asyncio.run(fast_tool_calls("比亚迪最近成交量有放大吗"))
        planned = {c["name"] for c in (plan or [])}
        check("量能 -> 行情+历史序列",
              {"get_quote", "get_history_daily"} <= planned, str(plan))

        check("对比类交还 LLM", asyncio.run(fast_tool_calls("宁德时代和比亚迪哪个好")) is None)
        check("概念题交还 LLM", asyncio.run(fast_tool_calls("市盈率是什么意思")) is None)
        check("标的解析不出时交还 LLM",
              asyncio.run(fast_tool_calls("这个板块现在什么价")) is None)
    finally:
        entity._stock_names = orig


# ===== P20：回复去重与卡片时序（2026-10-27）=====
def test_reply_dedup_and_card_order():
    print("\n[P20] 重复内容/重复卡片去重，卡片改到正文之后")
    from agent.core.sanitize import dedupe_answer_text
    from agent.core.state import recent_turns

    # 1. 正文：完全重复的段落只留一段
    para = "贵州茅台站上 5 日均线，短期走势偏强，量能温和放大，需留意上方压力位。"
    text = para + "\n\n估值处于近三年中枢附近，单看倍数容易误判。\n\n" + para
    out = dedupe_answer_text(text)
    check("重复段落只留一段", out.count("站上 5 日均线") == 1, out)
    check("非重复段落原样保留", "估值处于近三年中枢附近" in out, out)
    check("无重复时逐字不变", dedupe_answer_text("只有一段话。") == "只有一段话。")
    check("短句重复不误删", dedupe_answer_text("结论：\n\n结论：") == "结论：\n\n结论：")

    # 2. 工具结果：同一工具 + 同一入参重复调用只留最后一次成功结果
    args = {"code": "SH600519"}
    tr = [
        {"tool": "get_quote", "args": args,
         "result": {"ok": True, "data": {"code": "SH600519", "price": 1}}},
        {"tool": "get_quote", "args": args,
         "result": {"ok": False, "error": "timeout"}},
        {"tool": "get_quote", "args": args,
         "result": {"ok": True, "data": {"code": "SH600519", "price": 2}}},
    ]
    deduped = _dedupe_tool_results(tr)
    check("重复工具结果折叠成一条", len(deduped) == 1, str(deduped))
    check("保留最后一次成功结果", deduped[0]["result"]["data"]["price"] == 2, str(deduped))

    # 后面的失败不能把已经取到的数据冲掉
    tr_fail_last = [tr[0], tr[1]]
    deduped2 = _dedupe_tool_results(tr_fail_last)
    check("失败不覆盖已成功的数据",
          len(deduped2) == 1 and deduped2[0]["result"].get("ok"), str(deduped2))

    # 不同入参（不同标的）不能被误合并
    other = {"tool": "get_quote", "args": {"code": "SZ000001"},
             "result": {"ok": True, "data": {"code": "SZ000001", "price": 9}}}
    check("不同入参不合并", len(_dedupe_tool_results([tr[0], other])) == 2)

    # 3. 同类卡片只留一份候选（重复调用同一工具也是）
    quote = {"tool": "get_quote",
             "result": {"ok": True,
                        "data": {"code": "SH600519", "name": "贵州茅台", "price": 1500.0}}}
    cands = _card_candidates([quote, dict(quote)], {})
    check("同类候选只留一份", len(cands) == 1 and cands[0][0] == "quote_card", str(cands))

    emitted = []
    orig_card, orig_table = stock_cards.emit_card, stock_cards.emit_table
    stock_cards.emit_card = lambda t, p: emitted.append(t)
    stock_cards.emit_table = lambda t, p: emitted.append(t)
    try:
        got = stock_cards._emit_structured({"query": "茅台现在什么价"}, [quote, dict(quote)])
        check("一次回复只发一张卡", got == ["quote_card"] and emitted == ["quote_card"],
              f"{got} / {emitted}")
        again = stock_cards._emit_structured(
            {"query": "茅台现在什么价", "cards": ["quote_card"]}, [quote])
        check("同一轮重复合成不重复发卡", again == [] and emitted == ["quote_card"],
              f"{again} / {emitted}")
    finally:
        stock_cards.emit_card, stock_cards.emit_table = orig_card, orig_table

    # 4. 事件卡：同一条资讯被多频道重复返回时只渲染一条
    news = [
        {"title": "茅台回购公告", "datetime": "2026-10-23 10:00"},
        {"title": " 茅台回购公告 ", "datetime": "2026-10-23 11:00"},
    ]
    payload = stock_cards._event_card_payload(news)
    check("事件卡按标题去重", payload and len(payload["events"]) == 1, str(payload))

    # 5. 历史：重复出现的同一段回答只喂给模型一次
    dupe = "这是一段足够长的历史回答，重复出现时会让模型跟着复述一遍同样的内容。"
    hist = recent_turns({"messages": [
        HumanMessage(content="q1"),
        AIMessage(content=dupe),
        AIMessage(content=dupe),
        HumanMessage(content="q2"),
    ]}, 6)
    check("历史重复回答只保留一条",
          sum(1 for m in hist if isinstance(m, AIMessage)) == 1, str(hist))

    # 6. 时序：token 先全部下发，卡片最后才发
    import asyncio

    from langchain_core.messages import AIMessageChunk

    class _FakeStreamLLM:
        async def astream(self, msgs):
            yield AIMessageChunk(content="第一段正文，先说结论。\n\n")
            yield AIMessageChunk(content="第二段正文，补充依据。")

    order = []
    orig_emit_fn, orig_stream_llm = stock_mod.emit, stock_mod.get_llm
    orig_ecard, orig_etable = stock_cards.emit_card, stock_cards.emit_table
    stock_mod.get_llm = lambda **kw: _FakeStreamLLM()
    stock_mod.emit = lambda ev, data: order.append(ev)
    stock_cards.emit_card = lambda t, p: order.append("card")
    stock_cards.emit_table = lambda t, p: order.append("table")
    try:
        out3 = asyncio.run(stock_mod.synthesize({
            "query": "茅台现在什么价",
            "tool_results": [quote],
        }))
    finally:
        stock_mod.emit, stock_mod.get_llm = orig_emit_fn, orig_stream_llm
        stock_cards.emit_card, stock_cards.emit_table = orig_ecard, orig_etable
    check("卡片排在正文之后（最后一帧）",
          order and order[-1] == "card" and order.count("token") >= 1, str(order))
    check("synthesize 回报已发卡片类型", out3.get("cards") == ["quote_card"],
          str(out3.get("cards")))


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
    test_rag_coverage_gate()
    test_rag_coverage_downgrade()
    test_supervisor_merge()
    test_entity_space_name()
    test_consult_routing()
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
    test_meta_narration_filtered()
    test_prompt_no_process_narration()
    test_event_card_desc_clean()
    test_fast_plan_and_chat_shortcut()
    test_reply_dedup_and_card_order()
    print("\n" + "=" * 62)
    if _FAILED:
        print(f"FAILED: {len(_FAILED)} 项 -> {_FAILED}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
