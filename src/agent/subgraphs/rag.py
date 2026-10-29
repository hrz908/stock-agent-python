r"""RAG 混合检索子图（RAG 漏斗）。

流程: hybrid_retrieve -> sufficiency_check --(命中)--> generate_with_citations -> END
                                         |--(不足)--> escalate(概念->chat，其余->stock_agent) -> END
                                         |--(点名标的无语料·问原文)--> answer_not_covered -> END
                                         \--(点名标的无语料·问当下状态)--> escalate -> stock_agent

sufficiency_check 判定顺序：
1) 覆盖闸（最前）：问题点名的标的在语料库里一条文档都没有 -> 问原文（年报 / 公告正文）
   或问没有工具口径的内容时出固定话术，不生成、不取数；问「现在怎么样」的一类
   降级到取数子图，corpus_miss 一并带过去，由 stock 子图在合成时说明语料边界。
   这一步必须排在阈值闸之前：池外标的的检索结果本来就是一堆别家的
   高分片段，靠分数判不出来，只有查库能确定「它在不在范围内」。
2) 阈值闸（重排分 < RERANK_THRESHOLD 或有效片段 < 2 即未命中；重排关闭时回退
   余弦相似度 < RAG_THRESHOLD）；
3) 实体一致性（点名 A 却只命中带代码的别家片段 -> 未命中，别把别家数据说成用户的标的）；
4) 高分短路（最高重排分 >= RAG_JUDGE_SKIP_SCORE 直接判命中，省一次小模型判读）；
5) 其余过阈值样本交小模型判读「能否回答」（prompts/rag_judge.xml），避免主题相似但答非所问
   （问实时股价却检索到投教文章）。
未命中时概念问题置 intent=chat（通用知识直接回答），其余置 intent=stock_agent，由主图重路由。

generate_with_citations: 用 prompts/rag.xml 生成带 [n] 引用的回答，并发 sources 事件
（{"docs":[{title, chunk_id, score}]}，与前端和 Go 事件协议一致）。
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agent.config import (
    ANSWER_ENABLE_THINKING,
    RAG_JUDGE_SKIP_SCORE,
    RAG_THRESHOLD,
    RAG_TOP_K,
    RERANK_THRESHOLD,
)
from agent.core.keywords import is_concept_question
from agent.core.llm import get_llm, get_small_llm
from agent.core.sanitize import StreamCleaner, dedupe_answer_text
from agent.core.state import AgentState, recent_turns
from agent.core.template import render
from agent.rag import coverage
from agent.rag.entity import entity_codes, entity_mentions
from agent.rag.retriever import build_retriever
from agent.subgraphs import emit

logger = logging.getLogger("stocka.agent")

_MIN_CHUNKS = 2
_retriever = None


def _get_retriever():
    global _retriever
    if _retriever is None:
        _retriever = build_retriever(k=RAG_TOP_K)
    return _retriever


# 有实时工具能取到的资料类型：池外标的问这些时不该回「没覆盖」。
# 研报/新闻各有对应工具（get_research_reports / get_stock_news），财报、行情、资金
# 已在 _fallback._DATA_KEYWORDS 里覆盖，这里只补它漏掉的词。
_TOOL_SERVABLE = ("研报", "研究报告", "新闻", "资讯")

# 只能靠语料原文的问句：没有语料就是没有答案，取数工具也补不上，
# 降级只会白跑一轮 tool_call（8~16s）再回一句「没有数据」。
_DOC_TEXT_ONLY = (
    "年报", "半年报", "一季报", "三季报", "定期报告", "公告原文", "公告正文",
    "原文", "招股书", "招股说明书", "募集说明书", "财报原文",
)

# 「这只票现在什么情况」这类问句：命中覆盖闸时仍有行情 / 资金 / 快讯可答，
# 降级到取数子图比甩一句「不在范围内」有用。判据只认「当下状态」语义——
# 业务构成 / 竞争格局 / 公司战略这类没有工具口径的问题不在其中，仍出话术。
_OBSERVABLE_STATE = (
    "现在", "今天", "今日", "目前", "当下", "最近", "近况", "走势", "表现",
    "情况", "怎么样", "怎样", "如何", "进展", "动态", "状态", "近几天",
)

_NOT_COVERED_TEXT = {
    # 不在池内：说清范围，别让用户以为资料丢了
    "out_of_scope": (
        "关于 {subject}：{unit}不在当前的文本语料范围内（覆盖成交额前 300 只）。"
        "库里没有它的年报、公告或研报原文，我不凭印象下结论。\n\n"
        "实时数据不受影响，可以这样问：「{subject} 现在什么价」"
        "「{subject} 最新一期财报的营收和净利润」「{subject} 最近有哪些机构研报」，"
        "也可以换一只标的。"
    ),
    # 在池内但还没采到：回填期间这是常态，说「稍等」比说「不在范围」准确
    "pending": (
        "关于 {subject}：{unit}的文本资料还没入库（历史语料正在补齐）。"
        "现在只有实时数据，年报、公告正文要等采集跑到这里。\n\n"
        "期间可以这样问：「{subject} 现在什么价」"
        "「{subject} 最新一期财报的营收和净利润」「{subject} 最近有哪些机构研报」。"
    ),
}

# 固定话术按打字机节奏发，前端呈现自然的流式效果（与 refuse 节点一致）
_CHUNK = 16
_CHUNK_INTERVAL = 0.02


def _asks_current_state(state: dict) -> bool:
    """问句问的是这只票当下的状态：行情 / 资金 / 快讯能部分作答。"""
    text = (state.get("query") or "") + " " + (state.get("rewritten") or "")
    return any(k in text for k in _OBSERVABLE_STATE)


def _wants_document_text(state: dict) -> bool:
    """问句要的是原文（年报 / 公告正文）：这类内容取数工具没有口径。"""
    text = (state.get("query") or "") + " " + (state.get("rewritten") or "")
    return any(k in text for k in _DOC_TEXT_ONLY)


def _wants_tool_data(state: dict) -> bool:
    """问题能用取数工具答（行情/财报/研报/新闻）时，不能拦成「没覆盖」。"""
    from agent.subgraphs.stock._fallback import _needs_data_fetch

    text = (state.get("query") or "") + " " + (state.get("rewritten") or "")
    return any(k in text for k in _TOOL_SERVABLE) or _needs_data_fetch(state)


async def answer_not_covered(state: dict) -> dict:
    """点名标的没有语料：固定话术说明采集范围，不生成、不调工具、不出引用。

    固定文案而非让模型组织：这类回答的价值在于"准确说明边界"，
    交给模型反而会补一句「不过据我了解…」，把边界又糊掉。
    """
    t0 = time.monotonic()
    codes = state.get("corpus_miss") or []
    subject = "、".join(codes) or "该标的"
    unit = "这只标的" if len(codes) <= 1 else "这些标的"
    reason = state.get("corpus_miss_reason") or "out_of_scope"
    text = _NOT_COVERED_TEXT.get(reason, _NOT_COVERED_TEXT["out_of_scope"]).format(
        subject=subject, unit=unit)
    for i in range(0, len(text), _CHUNK):
        emit("token", {"delta": text[i:i + _CHUNK]})
        await asyncio.sleep(_CHUNK_INTERVAL)
    return {
        "messages": [AIMessage(content=text)],
        "citations": [],
        "timings": {"not_covered_ms": int((time.monotonic() - t0) * 1000)},
    }


async def hybrid_retrieve(state: dict) -> dict:
    t0 = time.monotonic()
    query = state.get("rewritten") or state.get("query", "") or ""
    timings: dict = {}
    # 问题点名了某只股票时给它软偏好，而不是硬过滤：语料扩到几百篇后同名研报/点评
    # 很多，不压一下目标公司的片段会被别家挤掉；但「借公司名问行业/政策」的查询不能
    # 被筛空。CASE 前缀让命中代码的块在两路召回里排到前面、全库块兜底填满候选池，
    # 一趟 SQL + 一次重排即可，不再走「先按代码过滤、不足再全库重跑」的第二趟。
    codes = await entity_codes(query)
    try:
        docs = await _get_retriever().asearch(
            query, prefer_codes=codes, timings=timings)
    except Exception as e:  # noqa: BLE001 - 检索失败按“不足”处理，交给 escalate
        timings["retrieve_ms"] = int((time.monotonic() - t0) * 1000)
        return {"retrieved": [], "corpus_miss": [], "corpus_miss_reason": "",
                "error": f"retrieve_failed: {e}", "timings": timings}
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
    # corpus_miss 每轮必须显式清空：state 随 checkpointer 跨轮保留，不清的话
    # 上一轮「这只票没覆盖」会一直命中下去，后面换了标的也还是那句固定话术
    return {"retrieved": retrieved, "corpus_miss": [], "corpus_miss_reason": "",
            "timings": timings}


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

    四级判定，顺序有讲究：
    1) 覆盖闸（最便宜且最确定）——点名标的在语料库里没有任何文档时直接说明范围；
    2) 阈值闸——最高分低于阈值或有效片段 < 2 即未命中；
    3) 实体一致性——点名 A 却只命中带代码的别家片段，按未命中处理；
    4) LLM 判读——过阈值的片段交小模型判「能否回答」，判读异常按命中处理、不阻断链路。
    分数优先用 rerank_score（交叉编码器，绝对可分），重排关闭时回退 similarity
    （余弦在向量空间里有很高的地板值，只能做粗筛，不能做精确判定）。
    """
    t0 = time.monotonic()
    retrieved = state.get("retrieved", []) or []
    query = state.get("rewritten") or state.get("query", "") or ""
    # 未命中时的降级目标：概念/投教问题不需要取数，交给 chat 用通用知识直接回答；
    # 其余问题按原设计升级到取数 agent
    miss_intent = "chat" if is_concept_question(query) else "stock_agent"
    # 实体识别提前到最前面：覆盖闸要用，后面的实体一致性校验复用同一份结果
    # （entity_mentions 按问句缓存，retriever 里刚查过同一条问句，这里是命中缓存）
    mentions = await entity_mentions(query)
    codes = set(mentions)
    # 覆盖闸：点名标的在库里一条文档都没有 -> 说明现状，而不是升级取数让模型自由发挥。
    # 必须排在阈值闸之前——池外标的的候选本来就是一堆别家公司的高分片段，靠分数判不出来；
    # 而且能省掉后面那次小模型判读。要取数的问句（行情/财报/研报/新闻）另有实时工具，不拦。
    if codes and not _wants_tool_data(state):
        try:
            info = await coverage.detail(mentions)
        except Exception as exc:  # noqa: BLE001 - 判定失败不能拖垮命中闸
            logger.warning("覆盖判定失败，跳过该闸：%s", exc)
            info = {}
        # info 为空 = 判定不可用（库抖动/表未建），fail-open 交回常规检索
        if info and not any(has_docs for has_docs, _ in info.values()):
            reason = "pending" if any(pool for _, pool in info.values()) else "out_of_scope"
            patch = {
                "corpus_miss": mentions,
                "corpus_miss_reason": reason,
                "timings": {"sufficiency_ms": int((time.monotonic() - t0) * 1000)},
            }
            # 问原文 / 问没有工具口径的内容：没有语料就是没有答案，出话术。
            # 问「现在怎么样」的一类还有行情 / 资金 / 快讯可答，降级到取数子图
            # （corpus_miss 一并带过去，由 stock 子图在合成时说明边界）。
            if _wants_document_text(state) or not _asks_current_state(state):
                logger.info("点名标的无语料（%s），出未覆盖话术：%s", reason, mentions)
                return patch
            logger.info("点名标的无语料（%s），降级取数：%s", reason, mentions)
            return {**patch, "intent": "stock_agent"}
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
    t_judge = time.monotonic()
    can_answer = await _judge_can_answer(query, retrieved)
    timings = {
        "sufficiency_ms": int((time.monotonic() - t0) * 1000),
        "judge_ms": int((time.monotonic() - t_judge) * 1000),
    }
    if can_answer:
        return {"timings": timings}
    return {"intent": miss_intent, "timings": timings}


def route_after_check(state: dict) -> Literal["escalate", "generate", "not_covered"]:
    """覆盖闸命中且没被降级 -> 固定话术；其余按 intent 分派。

    点名标的无语料有两种去向：问原文 / 问没有工具口径的内容，intent 保持 stock，
    走 not_covered；问当下状态，sufficiency_check 会把 intent 改成 stock_agent，
    这里按普通升级走 escalate，由主图重路由到取数子图。
    判「有没有被降级」而不是判 intent == 'stock'：节点返回的只是增量 patch，
    存量 intent 不一定在里面。
    """
    if state.get("corpus_miss") and state.get("intent") != "stock_agent":
        return "not_covered"
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
    last_usage = None
    async for chunk in llm.astream(messages):
        if isinstance(chunk.content, str) and chunk.content:
            delta = cleaner.feed(chunk.content)
            if delta:
                if ttft_ms is None:
                    ttft_ms = int((time.monotonic() - t0) * 1000)
                sent.append(delta)
                emit("token", {"delta": delta})
        # 只认最后一块的累计用量，避免 chunk 相加把 token 数放大几个数量级
        um = getattr(chunk, "usage_metadata", None)
        if um:
            last_usage = um
        parts.append(chunk)
    tail = cleaner.flush()
    if tail:
        sent.append(tail)
        emit("token", {"delta": tail})
    ai = parts[0] if parts else None
    for p in parts[1:]:
        ai = ai + p
    if ai is not None:
        patch = {"content": dedupe_answer_text("".join(sent))}
        if last_usage:
            patch["usage_metadata"] = last_usage
        ai = ai.model_copy(update=patch)
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
    g.add_node("not_covered", answer_not_covered)

    g.add_edge(START, "hybrid_retrieve")
    g.add_edge("hybrid_retrieve", "sufficiency_check")
    g.add_conditional_edges("sufficiency_check", route_after_check, {
        "escalate": "escalate",
        "generate": "generate",
        "not_covered": "not_covered",
    })
    g.add_edge("escalate", END)
    g.add_edge("generate", END)
    g.add_edge("not_covered", END)
    # 无 interrupt、无跨调用记忆：不单独落 checkpoint，省一次全量 state 写库
    return g.compile(checkpointer=False)
