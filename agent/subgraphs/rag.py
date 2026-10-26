r"""RAG 混合检索子图（RAG 漏斗）。

流程: hybrid_retrieve -> sufficiency_check --(命中)--> generate_with_citations -> END
                                         \--(不足)--> escalate(概念->chat，其余->stock_agent) -> END

sufficiency_check 三级判定：阈值闸（重排分 < RERANK_THRESHOLD 或有效片段 < 2 即未命中；
重排关闭时回退余弦相似度 < RAG_THRESHOLD）；
高分短路（最高重排分 >= RAG_JUDGE_SKIP_SCORE 直接判命中，省一次小模型判读）；
其余过阈值样本交小模型判读「能否回答」（prompts/rag_judge.xml），避免主题相似但答非所问
（问实时股价却检索到投教文章）。未命中时概念问题置 intent=chat（通用知识直接回答），
其余置 intent=stock_agent，由主图重路由。
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

from config import (
    ANSWER_ENABLE_THINKING,
    RAG_JUDGE_SKIP_SCORE,
    RAG_THRESHOLD,
    RAG_TOP_K,
    RERANK_THRESHOLD,
)
from core.keywords import is_concept_question
from core.llm import get_llm, get_small_llm
from core.sanitize import StreamCleaner
from core.state import AgentState, recent_turns
from core.template import render
from rag.entity import entity_codes
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
    # 问题点名了某只股票时先按代码过滤：语料扩到几百篇后，同名研报/点评很多，
    # 不过滤会让别家公司的片段挤掉目标公司。识别不出实体时按全库检索。
    codes = await entity_codes(query)
    try:
        docs = await _get_retriever().asearch(
            query, stock_codes=codes, timings=timings)
        # 过滤后片段太少或最高分不过阈值：这次查询可能只是借公司名问行业/政策，
        # 回退全库检索，避免实体过滤把可用资料筛空
        if codes:
            has_rerank = any(d.metadata.get("rerank_score") is not None for d in docs)
            top = max(
                ((d.metadata.get("rerank_score") or 0.0) if has_rerank
                 else (d.metadata.get("similarity") or 0.0)
                 for d in docs),
                default=0.0,
            )
            th = RERANK_THRESHOLD if has_rerank else RAG_THRESHOLD
            if len(docs) < _MIN_CHUNKS or top < th:
                fb: dict = {}
                docs = await _get_retriever().asearch(query, timings=fb)
                for key, value in fb.items():
                    timings[key] = timings.get(key, 0) + value
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
        # 命中闸的实体一致性校验用（文件类文档为空列表）
        "stock_codes": d.metadata.get("stock_codes") or [],
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
    query = state.get("rewritten") or state.get("query", "") or ""
    # 未命中时的降级目标：概念/投教问题不需要取数，交给 chat 用通用知识直接回答；
    # 其余问题按原设计升级到取数 agent
    miss_intent = "chat" if is_concept_question(query) else "stock_agent"
    has_rerank = any(r.get("rerank_score") is not None for r in retrieved)
    if has_rerank:
        top_score = max((r.get("rerank_score") or 0.0 for r in retrieved), default=0.0)
        threshold = RERANK_THRESHOLD
    else:
        top_score = max((r.get("similarity", 0.0) for r in retrieved), default=0.0)
        threshold = RAG_THRESHOLD
    if len(retrieved) < _MIN_CHUNKS or top_score < threshold:
        return {
            "intent": miss_intent,
            "timings": {"sufficiency_ms": int((time.monotonic() - t0) * 1000)},
        }
    # 实体一致性：问题点名了某只股票（代码/全称），但所有带代码的片段都属于别家公司，
    # 说明检索只是「主题相似」——交给判读或直接作答都会把别家数据说成用户的标的，
    # 直接按未命中处理，升级取数。文件类/快讯类片段没有代码，不参与该判定。
    codes = await entity_codes(query)
    if codes:
        coded = [r for r in retrieved if r.get("stock_codes")]
        if coded and not any(set(r.get("stock_codes") or []) & codes for r in coded):
            logger.info("实体不一致，按未命中处理：query=%s 命中代码=%s",
                        query[:40], sorted({c for r in coded for c in r.get("stock_codes") or []}))
            return {
                "intent": miss_intent,
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
    return {"intent": miss_intent, "timings": timings}


def route_after_check(state: dict) -> Literal["escalate", "generate"]:
    # 未命中时 sufficiency_check 会把 intent 改成 stock_agent（取数）或 chat（概念通用回答）
    return "generate" if state.get("intent") == "stock" else "escalate"


async def escalate(state: dict) -> dict:
    """占位节点：intent 已被 sufficiency_check 改为 stock_agent / chat，直接结束子图。"""
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

    llm = get_llm(streaming=True, temperature=0.3, thinking=ANSWER_ENABLE_THINKING)
    # 流式生成：逐块 emit token，同时累加成完整 AIMessage 写回 state。
    # 漏出的提示词标签（<user_query> 等）在 cleaner 里剥掉，正文原样透传
    cleaner = StreamCleaner()
    parts = []
    sent = []
    ttft_ms = None
    async for chunk in llm.astream(messages):
        if isinstance(chunk.content, str) and chunk.content:
            delta = cleaner.feed(chunk.content)
            if delta:
                if ttft_ms is None:
                    ttft_ms = int((time.monotonic() - t0) * 1000)
                sent.append(delta)
                emit("token", {"delta": delta})
        parts.append(chunk)
    tail = cleaner.flush()
    if tail:
        sent.append(tail)
        emit("token", {"delta": tail})
    ai = parts[0] if parts else None
    for p in parts[1:]:
        ai = ai + p
    if ai is not None:
        ai = ai.model_copy(update={"content": "".join(sent)})
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
