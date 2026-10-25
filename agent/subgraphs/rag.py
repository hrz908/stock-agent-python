r"""RAG 混合检索子图（RAG 漏斗）。

流程: hybrid_retrieve -> sufficiency_check --(命中)--> generate_with_citations -> END
                                         \--(不足)--> escalate(设 intent=stock_agent) -> END

sufficiency_check 两级判定：阈值闸（重排分 < RERANK_THRESHOLD 或有效片段 < 2 即未命中；
重排关闭时回退余弦相似度 < RAG_THRESHOLD）；
过阈值后再交小模型判读「能否回答」（prompts/rag_judge.xml），避免主题相似但答非所问
（问实时股价却检索到投教文章）。未命中时置 intent=stock_agent，由主图重路由到炒股 agent 子图。
generate_with_citations: 用 prompts/rag.xml 生成带 [n] 引用的回答，并发 sources 事件
（{"docs":[{title, chunk_id, score}]}，与前端和 Go 事件协议一致）。
"""
import logging
import time
from datetime import datetime
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from config import RAG_JUDGE_SKIP_SCORE, RAG_THRESHOLD, RAG_TOP_K, RERANK_THRESHOLD
from core.llm import get_llm, get_small_llm
from core.state import AgentState, recent_turns
from core.template import render
from rag.retriever import build_retriever
from subgraphs import emit

logger = logging.getLogger("stocka.agent")

_MIN_CHUNKS = 2
_retriever = None


def _get_retriever():
    global _retriever
    if _retriever is None:
        _retriever = build_retriever(k=RAG_TOP_K)
    return _retriever


async def hybrid_retrieve(state: dict) -> dict:
    t0 = time.monotonic()
    query = state.get("rewritten") or state.get("query", "") or ""
    timings: dict = {}
    try:
        docs = await _get_retriever().asearch(query, timings=timings)
    except Exception as e:  # noqa: BLE001 - 检索失败按“不足”处理，交给 escalate
        timings["retrieve_ms"] = int((time.monotonic() - t0) * 1000)
        return {"retrieved": [], "error": f"retrieve_failed: {e}", "timings": timings}
    timings["retrieve_ms"] = int((time.monotonic() - t0) * 1000)
    retrieved = [{
        "content": d.page_content,
        "title": d.metadata.get("title", ""),
        "source_path": d.metadata.get("source_path", ""),
        "chunk_id": str(d.metadata.get("id", "")),
        "score": d.metadata.get("score", 0.0),
        "similarity": d.metadata.get("similarity", 0.0),
        # 重排关闭时为 None，命中闸回退到 similarity
        "rerank_score": d.metadata.get("rerank_score"),
    } for d in docs]
    return {"retrieved": retrieved, "timings": timings}


class _Judge(BaseModel):
    """RAG 命中判读的结构化输出契约（与 prompts/rag_judge.xml 规则对应）。"""

    can_answer: bool = Field(description="检索资料能否直接回答用户问题")
    reason: str = Field(default="", description="一句话理由")


# 判读上下文上限（字符）：只取片段开头，够判主题与信息量即可
_JUDGE_MAX_CHARS = 4000

# 结构化判读的 runnable 缓存：with_structured_output 每次都会重新包装 schema
_judge_runnable = None


def _get_judge_runnable():
    global _judge_runnable
    if _judge_runnable is None:
        _judge_runnable = get_small_llm(
            streaming=False, temperature=0.0
        ).with_structured_output(_Judge)
    return _judge_runnable


async def _judge_can_answer(query: str, retrieved: list) -> bool:
    """LLM 判读检索片段能否回答用户问题。

    判官缺席（模型异常 / 结构化输出失败）时按「能」处理，退回纯阈值口径：
    宁可沿用原行为出引用回答，也不因判读故障把所有问题都甩给取数 agent。
    """
    sep = chr(10) * 2
    passages = sep.join(
        "[" + str(i) + "] 《" + str(r.get("title", "")) + "》"
        + str(r.get("content", ""))[:300]
        for i, r in enumerate(retrieved, 1)
    )[:_JUDGE_MAX_CHARS]
    try:
        verdict = await _get_judge_runnable().ainvoke([
            HumanMessage(content=render("rag_judge.xml", query=query, passages=passages))
        ])
        return bool(verdict.can_answer)
    except Exception as e:  # noqa: BLE001 - 判读失败不阻断，退回纯阈值口径
        logger.warning(
            "rag judge failed (%s: %s), fallback to threshold-only",
            type(e).__name__, e,
        )
        return True


async def sufficiency_check(state: dict) -> dict:
    """判断检索是否充分，不足则置 intent=stock_agent 让主图重路由。

    两级判定：阈值闸（便宜）——最高分低于阈值或有效片段 < 2 即未命中；
    LLM 判读（准）——过阈值的片段交小模型判「能否回答」，判读异常按命中处理、不阻断链路。
    分数优先用 rerank_score（交叉编码器，绝对可分），重排关闭时回退 similarity
    （余弦在向量空间里有很高的地板值，只能做粗筛，不能做精确判定）。
    """
    t0 = time.monotonic()
    retrieved = state.get("retrieved", []) or []
    has_rerank = any(r.get("rerank_score") is not None for r in retrieved)
    if has_rerank:
        top_score = max((r.get("rerank_score") or 0.0 for r in retrieved), default=0.0)
        threshold = RERANK_THRESHOLD
    else:
        top_score = max((r.get("similarity", 0.0) for r in retrieved), default=0.0)
        threshold = RAG_THRESHOLD
    if len(retrieved) < _MIN_CHUNKS or top_score < threshold:
        return {
            "intent": "stock_agent",
            "timings": {"sufficiency_ms": int((time.monotonic() - t0) * 1000)},
        }
    # 高分短路：分数已远超阈值时判读没有增量信息（实测相关样本 top1 中位 1.0、
    # 负样本最大 0.884），直接命中省掉一次 2s 级小模型调用。
    # 只对重排分生效：余弦相似度地板值高（0.9 并不等于高相关），仍交给 judge。
    if has_rerank and RAG_JUDGE_SKIP_SCORE > 0 and top_score >= RAG_JUDGE_SKIP_SCORE:
        return {
            "timings": {
                "sufficiency_ms": int((time.monotonic() - t0) * 1000),
                "judge_ms": 0,
            },
        }
    query = state.get("rewritten") or state.get("query", "") or ""
    t_judge = time.monotonic()
    can_answer = await _judge_can_answer(query, retrieved)
    timings = {
        "sufficiency_ms": int((time.monotonic() - t0) * 1000),
        "judge_ms": int((time.monotonic() - t_judge) * 1000),
    }
    if can_answer:
        return {"timings": timings}
    return {"intent": "stock_agent", "timings": timings}


def route_after_check(state: dict) -> Literal["escalate", "generate"]:
    # 两级判定未命中后 intent 被改成 stock_agent
    return "escalate" if state.get("intent") == "stock_agent" else "generate"


async def escalate(state: dict) -> dict:
    """占位节点：intent 已在 sufficiency_check 改为 stock_agent，直接结束子图。"""
    return {}


async def generate_with_citations(state: dict) -> dict:
    t0 = time.monotonic()
    retrieved = state.get("retrieved", []) or []
    query = state.get("rewritten") or state.get("query", "") or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 给每个片段编号，供 [n] 引用
    ctx_lines = []
    citations = []
    for i, r in enumerate(retrieved, 1):
        ctx_lines.append(f"[{i}] 《{r.get('title','')}》\n{r.get('content','')}")
        citations.append({
            "index": i,
            "title": r.get("title", ""),
            "source_path": r.get("source_path", ""),
            "chunk_id": r.get("chunk_id", ""),
            "score": r.get("score", 0.0),
        })
    contexts = "\n\n".join(ctx_lines) if ctx_lines else "（无检索资料）"

    prompt = render("rag.xml", now=now, query=query, contexts=contexts)
    # 引用来源走独立 custom 事件（sources），不混入 token 流
    emit("sources", {"docs": [{
        "title": c.get("title", ""),
        "chunk_id": c.get("chunk_id", ""),
        "score": c.get("score", 0.0),
    } for c in citations]})

    # 带上最近历史让回答衔接上下文（复用 recent_turns 统一口径，跳过当前问题避免重复）
    messages = [SystemMessage(content=prompt)]
    messages.extend(recent_turns(state, 6, query=query))
    messages.append(HumanMessage(content=state.get("query", "") or query))

    llm = get_llm(streaming=True, temperature=0.3)
    # 流式生成：逐块 emit token，同时累加成完整 AIMessage 写回 state
    parts = []
    ttft_ms = None
    async for chunk in llm.astream(messages):
        if isinstance(chunk.content, str) and chunk.content:
            if ttft_ms is None:
                ttft_ms = int((time.monotonic() - t0) * 1000)
            emit("token", {"delta": chunk.content})
        parts.append(chunk)
    ai = parts[0] if parts else None
    for p in parts[1:]:
        ai = ai + p
    total_ms = int((time.monotonic() - t0) * 1000)
    return {
        "messages": [ai if ai is not None else AIMessage(content="")],
        "citations": citations,
        "timings": {
            "rag_ttft_ms": ttft_ms if ttft_ms is not None else total_ms,
            "rag_generate_ms": total_ms,
        },
    }


def build_rag_subgraph():
    g = StateGraph(AgentState)
    g.add_node("hybrid_retrieve", hybrid_retrieve)
    g.add_node("sufficiency_check", sufficiency_check)
    g.add_node("escalate", escalate)
    g.add_node("generate", generate_with_citations)

    g.add_edge(START, "hybrid_retrieve")
    g.add_edge("hybrid_retrieve", "sufficiency_check")
    g.add_conditional_edges("sufficiency_check", route_after_check, {
        "escalate": "escalate",
        "generate": "generate",
    })
    g.add_edge("escalate", END)
    g.add_edge("generate", END)
    # 无 interrupt、无跨调用记忆：不单独落 checkpoint，省一次全量 state 写库
    return g.compile(checkpointer=False)
