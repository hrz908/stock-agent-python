"""分类节点：意图路由。

用 small_llm + prompts/router.xml 输出意图与置信度：优先 with_structured_output，
失败回退 XML + parse_route；confidence < 0.6 强制降级 clarify（宁可反问，不可乱答），
解析失败一律 clarify。

意图五分：
- stock: 文档型股票问题，统一先进 RAG 漏斗——命中带引用作答，
  未命中由主图改写成 stock_agent 重路由；
- stock_agent: 实时数据问句（行情/估值/资金/龙虎榜/新闻等）直接进取数子图，
  跳过 RAG 的 embed + SQL + rerank；
- chat: 闲聊寒暄，直接陪聊，不挂工具；
- off_topic: 与股票无关的正经话题，走固定话术婉拒（nodes/refuse.py）；
- clarify: 问题含糊或低置信度，反问澄清。

关键词兜底先行：实时数据词命中即确定性判为 stock_agent；文档型词（财报/研报/公告）
仍判为 stock 走 RAG；概念型词（解释/区别/是什么）也留在 RAG。三条分支都不调
路由 LLM，既省一次调用，也防 LLM 把取数问句判错。
"""
import re
import time
from datetime import datetime

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from core.keywords import fast_chat, fast_intent, hit_data_keyword
from core.llm import get_small_llm
from core.template import load_template, parse_route, render
from nodes.rewrite import _format_history

_CONF_MIN = 0.6

# 指代不明的问句：「那个股票怎么样」「这只股票如何」——rewrite 无历史可消解时原样进入分类。
# 实测路由 LLM 会判成 stock，白跑一轮漏斗后给出「没有数据」（R-03）。这里加确定性护栏：
# 命中指代模式、不带 6 位代码、且是短问句 → 直接 clarify 反问。
_VAGUE_REF_RE = re.compile(
    r"(那个|这只|这个|那只|这支|那支|这只股|那只股|这个股|那个股)(只|个|支)?(股|股票)?"
)
_VAGUE_MAX_LEN = 24


def _is_vague_reference(query: str) -> bool:
    """未指明标的的短问句（指代 + 股票，无代码）。"""
    q = (query or "").strip()
    if not q or len(q) > _VAGUE_MAX_LEN:
        return False
    if not _VAGUE_REF_RE.search(q):
        return False
    if re.search(r"\d{6}", q):  # 带了代码就不算含糊
        return False
    return True


class _RouteDecision(BaseModel):
    """意图路由的结构化输出契约（与 router.xml 的 <route> 字段一一对应）。"""
    intent: str = Field(description="stock|chat|off_topic|clarify 之一")
    confidence: float = Field(description="对该判断的把握，0~1 的小数")
    reason: str = Field(default="", description="一句话理由")
    symbol: str = Field(default="", description="标的代码或名称，可空")
    market: str = Field(default="", description="SH|SZ|BJ|HK|US，可空")
    date_range: str = Field(default="", description="时间范围描述，可空")


# with_structured_output 会现构造一个 RunnableBinding（含 schema 转 JSON Schema），
# 每轮都构造一次纯属重复劳动；模型实例本身已被 core.llm 缓存
_route_runnable = None


def _get_route_runnable():
    global _route_runnable
    if _route_runnable is None:
        _route_runnable = get_small_llm(
            streaming=False, temperature=0.0
        ).with_structured_output(_RouteDecision)
    return _route_runnable


async def _route_decision(prompt: str) -> dict:
    """意图判定：优先结构化输出（with_structured_output），失败回退 XML 解析。

    两条路径都收敛到同一个 dict 形态（intent / confidence / entities），
    保证下游状态写入口径一致；任一路径异常都安全降级为 clarify。
    """
    small = get_small_llm(streaming=False, temperature=0.0)
    # 主路径：结构化输出，参数传递更稳、解析更可靠
    try:
        decision = await _get_route_runnable().ainvoke([HumanMessage(content=prompt)])
        return {
            "intent": decision.intent,
            "confidence": float(decision.confidence or 0.0),
            "entities": {
                "symbol": decision.symbol or "",
                "market": decision.market or "",
                "date_range": decision.date_range or "",
            },
        }
    except Exception:  # noqa: BLE001 - 结构化不可用（模型/版本）时退回 XML
        pass
    # 回退路径：沿用 XML + parse_route，保证兼容性
    try:
        resp = await small.ainvoke([HumanMessage(content=prompt)])
        r = parse_route(resp.content or "")
    except Exception:  # noqa: BLE001
        return {"intent": "clarify", "confidence": 0.0, "entities": {}}
    return {
        "intent": r.get("intent", "clarify"),
        "confidence": float(r.get("confidence", 0.0) or 0.0),
        "entities": r.get("entities", {}),
    }


def _hit_data_keyword(query: str) -> bool:
    return hit_data_keyword(query)


async def classify(state: dict) -> dict:
    t0 = time.monotonic()

    def done(payload: dict) -> dict:
        payload["timings"] = {
            "classify_ms": int((time.monotonic() - t0) * 1000),
        }
        return payload

    # 护栏已拦截时不再分类，直接透传 clarify
    if state.get("error"):
        return done({"intent": "clarify", "confidence": 0.0, "entities": {}})

    # 多轮场景下 rewrite 的合并路径已产出意图，这里只补计时直接透传，
    # 避免同一轮把路由提示词再跑一遍
    if state.get("intent"):
        return done({})

    query = state.get("rewritten") or state.get("query", "") or ""

    # 关键词兜底先行：命中实时数据词直接进取数子图，省一次 small_llm 调用，
    # 也省掉 RAG 的 embed + SQL + rerank；文档型（财报/研报/公告）仍走 RAG，
    # 概念型（解释/区别/是什么）也保持知识库回答，避免把「市盈率是什么」
    # 误送去取数。判定是确定性的，口径与 rewrite 的合并路由共用。
    fast = fast_intent(query)
    if fast:
        return done({"intent": fast, "confidence": 1.0, "entities": {}})

    # 问候寒暄：确定性判闲聊，省掉一次路由 LLM 往返（「你好」实测 1.8s）
    if fast_chat(query):
        return done({"intent": "chat", "confidence": 1.0, "entities": {}})

    # 指代不明兜底：「那个股票怎么样」无历史可消解时直接 clarify，不让路由 LLM 猜
    if _is_vague_reference(query):
        return done({"intent": "clarify", "confidence": 0.9, "entities": {}})

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    history = _format_history(state.get("messages", []))

    prompt = render("router.xml", now=now, history=history, query=query,
                    examples=load_template("router_examples.xml"))

    route = await _route_decision(prompt)
    intent = route["intent"]
    confidence = route["confidence"]
    if intent not in {"stock", "chat", "off_topic", "clarify"}:
        intent, confidence = "clarify", 0.0
    # 低置信度强制澄清
    if confidence < _CONF_MIN:
        intent = "clarify"

    return done({"intent": intent, "confidence": confidence, "entities": route["entities"]})
