"""Supervisor 节点：一次小模型调用完成「指代消解 + 意图路由」。

主图原来是 rewrite（改写）→ classify（路由）两个小模型节点：单轮跑两次，多轮才在
rewrite 里合并。这里统一成一个调度节点，无论单轮多轮都只调用一次——路由规则也只有
router.xml 一份口径。

上下文分三层喂给它：
1. 最近若干轮文本（_history_text，剔除本轮问题）；
2. Redis 滚动摘要（post 写入，补被裁掉的早期历史）；
3. 上一轮结构化上下文（agent:ctx:{tid}，post 写入：标的 / 链路）——行情卡走 custom
   事件、不进 messages，追问「它现在什么价」时仅靠文本历史可能找不到标的，第三层就是
   为这种情况准备的。

确定性快路径先行（实时数据词 / 问候 / 越界 / 指代不明且无上下文），命中即返回，
不惊动小模型；结构化输出失败回退 XML 解析，两条路径收敛到同一份 state 字段。
"""
import time
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.core.keywords import fast_chat, fast_intent, fast_off_topic
from agent.core.llm import get_small_llm
from agent.core.redis import get_redis
from agent.core.template import load_template, parse_route, render
from agent.nodes.classify import _is_vague_reference
from agent.nodes.rewrite import _MERGE_RULE, _MERGE_SYSTEM, _get_summary, _history_text

# 与 classify / rewrite 的低置信度口径保持一致
_CONF_MIN = 0.6
_INTENTS = {"stock", "chat", "off_topic", "clarify"}
_CTX_TTL = 7 * 24 * 3600


class _SupervisorDecision(BaseModel):
    """调度器的结构化输出契约（router.xml 的 route 字段 + 改写结果）。"""

    rewritten: str = Field(description="改写后不依赖上下文的独立问题")
    intent: str = Field(description="stock|chat|off_topic|clarify 之一")
    confidence: float = Field(description="对该判断的把握，0~1 的小数")
    reason: str = Field(default="", description="一句话理由")
    symbol: str = Field(default="", description="标的代码或名称，可空")
    market: str = Field(default="", description="SH|SZ|BJ|HK|US，可空")
    date_range: str = Field(default="", description="时间范围描述，可空")


_runnable = None


def _get_runnable():
    global _runnable
    if _runnable is None:
        _runnable = get_small_llm(
            streaming=False, temperature=0.0
        ).with_structured_output(_SupervisorDecision)
    return _runnable


async def load_context(thread_id: str) -> dict:
    """读上一轮结构化上下文；失败返回空 dict（上下文是增强项，不是前置依赖）。"""
    if not thread_id:
        return {}
    try:
        raw = await get_redis().hgetall(f"agent:ctx:{thread_id}")
    except Exception:  # noqa: BLE001 - 上下文不可用不影响路由
        return {}
    if not raw:
        return {}
    out = {}
    for key, value in raw.items():
        k = key.decode() if isinstance(key, bytes) else str(key)
        v = value.decode() if isinstance(value, bytes) else str(value)
        if v:
            out[k] = v
    return out


def _format_context(ctx: dict) -> str:
    """把结构化上下文压成一行，拼在 history 前面交给小模型。"""
    if not ctx:
        return ""
    parts = []
    if ctx.get("symbols"):
        parts.append(f"上一轮标的：{ctx['symbols']}")
    if ctx.get("rewritten"):
        parts.append(f"上一轮问题：{ctx['rewritten'][:40]}")
    if ctx.get("subgraph"):
        parts.append(f"上一轮链路：{ctx['subgraph']}")
    return "上下文：" + "；".join(parts) if parts else ""


def _clean_rewritten(text: str, query: str, symbol: str) -> str:
    """改写结果清洗：只取首行、剥引号；合并模型常把指代留在句里、只填 symbol。"""
    first = (text or "").strip().strip('"').strip().splitlines()
    rewritten = (first[0].strip() if first else "") or query
    if symbol and symbol not in rewritten:
        rewritten = f"{symbol} {rewritten}".strip()
    return rewritten


def _merge_decision(decision, query: str) -> dict:
    """把结构化结果收敛成 state 字段；关键词口径优先于模型判断。"""
    rewritten = _clean_rewritten(decision.rewritten, query, (decision.symbol or "").strip())
    intent = decision.intent if decision.intent in _INTENTS else "clarify"
    confidence = float(decision.confidence or 0.0)
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
    }


async def _route_only(prompt: str, query: str) -> dict:
    """结构化输出不可用时的回退：只判意图，改写原样透传。"""
    try:
        resp = await get_small_llm(streaming=False, temperature=0.0).ainvoke(
            [HumanMessage(content=prompt)]
        )
        route = parse_route(resp.content or "")
    except Exception:  # noqa: BLE001 - 路由失败一律澄清，不乱答
        return {"rewritten": query, "intent": "clarify", "confidence": 0.0, "entities": {}}
    intent = route.get("intent", "clarify")
    confidence = float(route.get("confidence", 0.0) or 0.0)
    if intent not in _INTENTS or confidence < _CONF_MIN:
        return {"rewritten": query, "intent": "clarify", "confidence": 0.0, "entities": {}}
    return {
        "rewritten": query,
        "intent": intent,
        "confidence": confidence,
        "entities": route.get("entities", {}),
    }


async def supervisor(state: dict) -> dict:
    t0 = time.monotonic()

    def done(payload: dict) -> dict:
        payload["timings"] = {"supervisor_ms": int((time.monotonic() - t0) * 1000)}
        return payload

    # 护栏已拦截：不再分类，直接澄清
    if state.get("error"):
        return done({"intent": "clarify", "confidence": 0.0, "entities": {}})
    # 上游（多轮合并路径 / 测试桩）已经给出意图：透传，不重复调用
    if state.get("intent"):
        return done({})

    query = state.get("query", "") or ""

    # 关键词兜底先行：确定性判定比小模型快一个量级，也防模型把取数问句判回 RAG
    fast = fast_intent(query)
    if fast:
        return done({"rewritten": query, "intent": fast, "confidence": 1.0, "entities": {}})
    if fast_chat(query):
        return done({"rewritten": query, "intent": "chat", "confidence": 1.0, "entities": {}})
    if fast_off_topic(query):
        return done({
            "rewritten": query, "intent": "off_topic", "confidence": 1.0, "entities": {},
        })

    thread_id = state.get("thread_id", "")
    ctx = await load_context(thread_id)

    # 指代不明且上下文里没有标的：反问澄清，不让模型猜
    if _is_vague_reference(query) and not ctx.get("symbols"):
        return done({
            "rewritten": query, "intent": "clarify", "confidence": 0.9, "entities": {},
        })

    history = _history_text(state)
    summary = await _get_summary(thread_id)
    ctx_text = _format_context(ctx)
    # 结构化上下文排在历史前面：卡片轮次没有文本可查，这一行就是唯一线索
    history_block = "\n".join(part for part in (ctx_text, history) if part)

    prompt = render(
        "router.xml",
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        history=history_block,
        query=query,
        examples=load_template("router_examples.xml"),
    ) + _MERGE_RULE
    bg = f"对话背景摘要：\n{summary}\n\n" if summary else ""

    try:
        decision = await _get_runnable().ainvoke([
            SystemMessage(content=_MERGE_SYSTEM),
            HumanMessage(content=bg + prompt),
        ])
        return done(_merge_decision(decision, query))
    except Exception:  # noqa: BLE001 - 结构化不可用时回退 XML 路由
        return done(await _route_only(bg + prompt, query))
