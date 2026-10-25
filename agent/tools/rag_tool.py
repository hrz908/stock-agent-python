"""知识库检索工具：把 RAG 混合检索封装成炒股 agent 可调用的 @tool。

设计（LangGraph 架构 §4.3）：炒股 agent 的工具集 = 数据工具（进程内 @tool）+ 本工具，
模型在 ReAct 循环里需要研报观点 / 概念解释时可主动补充检索，不必退出子图。
本工具不产数字：不进 stock 子图的 _DATA_TOOLS 白名单，不影响无数据兜底判定。
"""
from langchain_core.tools import tool

# 检索器进程级缓存（embedder 懒加载在 HybridRetriever 内部）
_retriever = None

# 一次检索最多取多少片段（调用方的 limit 在其基础上再截）
_RETRIEVER_K = 8


def _get_retriever():
    global _retriever
    if _retriever is None:
        from rag.retriever import build_retriever

        _retriever = build_retriever(k=_RETRIEVER_K)
    return _retriever


@tool
async def search_knowledge_base(
    query: str,
    limit: int = 4,
    stock_code: str = "",
    source_type: str = "",
    since: str = "",
) -> dict:
    """检索投研知识库（公告、年报季报、研报、政策、财经快讯、投教知识），返回最相关的文档片段。
    适合回答「公司公告说了什么 / 研报怎么看 / 某个政策影响 / 概念是什么意思」；
    行情、K线、估值、财报等实时数据请用对应的取数工具，不要用本工具。

    Args:
        query: 检索问题或关键词，如 "贵州茅台 回购进展"、"PE 和 PB 的区别"。
        limit: 返回片段数，默认 4，最多 8。
        stock_code: 可选，限定某只股票（如 "600519"）；只问公司相关问题时再传。
        source_type: 可选，限定资料类型：annual_report / semi_annual_report /
            quarterly_report / announcement / research_report / industry_policy /
            news_flash，也接受"年报/公告/研报/政策/快讯"等中文写法。
        since: 可选，发布时间下限 YYYY-MM-DD，如 "2025-01-01"。

    Returns:
        {"ok": True, "data": [{title, content, similarity, score, chunk_id, source_path}...]} 成功；
        {"ok": False, "error": "..."} 失败降级（知识库不可用时不阻塞其余工具）。
    """
    n = max(1, min(int(limit or 4), _RETRIEVER_K))
    try:
        docs = await _get_retriever().asearch(
            query, stock_code=stock_code, source_type=source_type, since=since
        )
    except Exception as e:  # noqa: BLE001 - 检索失败按工具降级处理
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "data": [{
        "title": d.metadata.get("title", ""),
        "content": d.page_content,
        "similarity": d.metadata.get("similarity", 0.0),
        # 引用事件用：chunk_id 对齐 sources 协议，score 优先重排分（与命中闸同尺度）
        "score": d.metadata.get("rerank_score") or d.metadata.get("similarity", 0.0),
        "chunk_id": str(d.metadata.get("id", "")),
        "source_path": d.metadata.get("source_path", ""),
    } for d in docs[:n]]}
