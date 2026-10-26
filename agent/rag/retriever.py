"""混合检索器：pgvector 向量召回 + PostgreSQL 全文检索，RRF(k=60) 融合，可选重排。"""
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_openai import OpenAIEmbeddings

from config import (
    DOC_FIRST_DOCS,
    DOC_FIRST_PER_DOC,
    DOC_FIRST_POOL,
    EMBED_DIM,
    ONLINE_EMBED_MODEL,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    RAG_PER_DOC,
    RAG_RERANK_FILTER,
    RERANK_CANDIDATES,
    RERANK_ENABLED,
    RERANK_THRESHOLD,
    RETRIEVAL_MODE,
)
from db import get_pool
from rag.reranker import rerank_scores
from rag.zh_tokenize import query_tsquery

logger = logging.getLogger("stocka.agent")

# 混合检索 SQL 模板（RRF k=60），用 psycopg 命名参数：
# 查询向量/全文 query 在模板里各出现多次，命名参数只传一份。
# {join}/{where_vec}/{where_lex} 由 _build_search_sql 按过滤条件拼装，无过滤时为空。
# similarity 必须按 chunk 自身向量算：只在 lex 命中的行不在 vec CTE 里，
# 若从 vec 取 dist 会得到 NULL→0，被 _clean_docs 的 sim_floor 整批丢掉。
SEARCH_SQL = """
WITH vec AS (
  SELECT c.id, (c.embedding <=> %(vec)s::vector) AS dist,
         ROW_NUMBER() OVER (ORDER BY c.embedding <=> %(vec)s::vector) AS rk
  FROM chunks c
  {join}
  {where_vec}
  ORDER BY c.embedding <=> %(vec)s::vector LIMIT %(inner_limit)s
),
lex AS (
  SELECT c.id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.tsv_zh, q) DESC) AS rk
  FROM chunks c
  CROSS JOIN to_tsquery('simple', %(q_ts)s) q
  {join}
  {where_lex}
  ORDER BY ts_rank_cd(c.tsv_zh, q) DESC LIMIT %(inner_limit)s
),
fused AS (
  SELECT id, SUM(1.0/(60+rk)) AS score
  FROM (SELECT id,rk FROM vec UNION ALL SELECT id,rk FROM lex) u GROUP BY id
)
SELECT c.id, c.content, d.title, d.source_path, d.id AS doc_id,
       a.stock_codes,
       f.score, (1 - (c.embedding <=> %(vec)s::vector)) AS similarity
FROM fused f
JOIN chunks c USING(id)
JOIN documents d ON d.id=c.document_id
-- 外层始终左连语料表：命中闸要做「实体一致性」校验（问题点名某只股票，
-- 结果却全是别家公司的研报时不能硬答）。文件 ingest 的文档没有对应行，
-- stock_codes 为 NULL，校验时按「无代码，不参与」处理。
LEFT JOIN ashare_documents a ON a.doc_id=c.document_id
ORDER BY f.score DESC, c.id LIMIT %(limit)s;
"""

# 与 ingest/ashare/types.py 的 SOURCE_TYPE_LABELS 对齐；agent 不挂载 ingest 目录，故本地维护
_SOURCE_TYPES = {
    "annual_report", "semi_annual_report", "quarterly_report", "announcement",
    "research_report", "industry_policy", "news_flash", "news",
}
# 模型常把类型写成中文，这里做一层别名收口；不在表里的值当作“不过滤”而不是报错
_SOURCE_TYPE_ALIASES = {
    "年报": "annual_report", "年度报告": "annual_report",
    "半年报": "semi_annual_report", "半年度报告": "semi_annual_report",
    "季报": "quarterly_report", "季度报告": "quarterly_report",
    "公告": "announcement", "公司公告": "announcement",
    "研报": "research_report", "研究报告": "research_report",
    "政策": "industry_policy", "行业政策": "industry_policy",
    "快讯": "news_flash", "新闻": "news",
}
_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_stock_code(value) -> str:
    """'sh600519' / '600519.SH' -> '600519'；不足 6 位视为无效。"""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-6:] if len(digits) >= 6 else ""


def _normalize_stock_codes(value) -> list[str]:
    """str / list -> 去重后的 6 位代码列表；空值返回 []。"""
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[str] = []
    for item in items:
        code = _normalize_stock_code(item)
        if code and code not in out:
            out.append(code)
    return out


def _normalize_source_type(value) -> str:
    text = str(value or "").strip().lower()
    text = _SOURCE_TYPE_ALIASES.get(text, text)
    return text if text in _SOURCE_TYPES else ""


def _normalize_since(value) -> str:
    """YYYY-MM-DD -> 'YYYY-MM-DD 00:00:00+08:00'，按北京时间当天零点过滤。"""
    text = str(value or "").strip()
    if not _SINCE_RE.match(text):
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return ""
    return f"{text} 00:00:00+08:00"


def _build_search_sql(
    stock_codes=None, source_type: str = "", since: str = ""
) -> tuple[str, dict]:
    """按过滤条件拼 SQL；过滤列都在 ashare_documents 上，文件 ingest 的文档不会被选中。

    过滤必须下推到 vec/lex 两个 CTE 内：先过滤再取 TopN，否则特定股票/类型的
    片段可能根本进不了候选池，外层再过滤只会得到空结果。
    stock_codes 支持单个代码或代码列表（问题里可能同时点名多家公司）。
    vec/lex 的候选上限由调用方在 params 里传 inner_limit（doc_first 需要更大的池）。
    """
    codes = _normalize_stock_codes(stock_codes)
    conditions: list[str] = []
    params: dict = {}
    if codes:
        # 单代码沿用原参数名（测试与既有调用口径不变）；多代码才走数组参数
        if len(codes) == 1:
            conditions.append("a.stock_codes && ARRAY[%(code)s]::text[]")
            params["code"] = codes[0]
        else:
            conditions.append("a.stock_codes && %(codes)s::text[]")
            params["codes"] = codes
    if source_type:
        conditions.append("a.source_type = %(stype)s")
        params["stype"] = source_type
    if since:
        conditions.append("a.pub_time >= %(since)s::timestamptz")
        params["since"] = since

    join = "JOIN ashare_documents a ON a.doc_id = c.document_id" if conditions else ""
    where_vec = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    where_lex = "WHERE " + " AND ".join(conditions + ["c.tsv_zh @@ q"])
    sql = SEARCH_SQL.format(join=join, where_vec=where_vec, where_lex=where_lex)
    return sql, params


def _vec_literal(embedding: list[float]) -> str:
    """list[float] -> pgvector 文本字面量 '[v0,v1,...]'。"""
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


# 进程级 embedder 单例：主图检索与 search_knowledge_base 工具各有一个 retriever 实例，
# 但底层的 OpenAIEmbeddings 客户端（含连接池）没有理由建两份
_embedder: OpenAIEmbeddings | None = None

# 查询向量缓存：同一问句的向量恒定，重复请求不必再打一次 embedding 接口。
# 典型命中场景：实体过滤后片段不足时 hybrid_retrieve 会用同一 query 全库重检；
# 一轮里 RAG 漏斗与知识库工具也可能搜同一句。按条数淘汰，不设 TTL（模型变更需重启）。
_EMBED_CACHE_MAX = 256
_EMBED_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()


async def embed_query_cached(query: str) -> list[float]:
    """取查询向量；命中缓存时省掉一次上游 embedding 往返（百毫秒级）。"""
    key = (ONLINE_EMBED_MODEL, query)
    hit = _EMBED_CACHE.get(key)
    if hit is not None:
        _EMBED_CACHE.move_to_end(key)
        return hit
    vec = await get_embedder().aembed_query(query)
    _EMBED_CACHE[key] = vec
    if len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
        _EMBED_CACHE.popitem(last=False)
    return vec


def get_embedder() -> OpenAIEmbeddings:
    global _embedder
    if _embedder is None:
        _embedder = OpenAIEmbeddings(
            model=ONLINE_EMBED_MODEL,
            openai_api_base=OPENAI_API_BASE,
            openai_api_key=OPENAI_API_KEY,
            # 必须显式指定：Qwen3-4B 默认输出 2560 维，与库里的 1536 不匹配
            dimensions=EMBED_DIM,
        )
    return _embedder


class HybridRetriever(BaseRetriever):
    """向量 + 全文 RRF 融合检索器。

    k: 进上下文的片段数上限。candidates: 送重排的候选池大小（小于 k 无意义）。
    查询向量走进程级单例客户端 + 缓存（见 embed_query_cached），实例本身无额外状态。
    """

    k: int = 6
    candidates: int = RERANK_CANDIDATES
    per_doc: int = RAG_PER_DOC

    class Config:
        arbitrary_types_allowed = True

    async def _run_candidates(
        self,
        query: str,
        timings: dict | None = None,
        *,
        stock_code: str = "",
        stock_codes=None,
        source_type: str = "",
        since: str = "",
    ) -> list[Document]:
        """召回 + 重排打分，返回未截断的候选池。

        保持 RRF 序而非重排分序：线上 limit=N 拿到的是 RRF 前 N 条，
        候选池扫描靠这个顺序切片重放，顺序混了重放结果就和真实执行对不上。
        timings 传入时回填 embed_ms / sql_ms / rerank_ms。
        stock_code / stock_codes / source_type / since 是可选过滤，下推到 SQL 两路召回里。
        """
        t0 = time.monotonic()
        emb = await embed_query_cached(query)
        if timings is not None:
            timings["embed_ms"] = int((time.monotonic() - t0) * 1000)
        vl = _vec_literal(emb)
        limit = max(self.k, self.candidates) if RERANK_ENABLED else self.k
        # chunk 模式只需 limit 个块；doc_first 要先从更大的 RRF 池按文档聚合
        inner_limit = max(limit, DOC_FIRST_POOL) if RETRIEVAL_MODE == "doc_first" else limit
        codes = _normalize_stock_codes(stock_codes if stock_codes else stock_code)
        sql, filter_params = _build_search_sql(
            stock_codes=codes,
            source_type=_normalize_source_type(source_type),
            since=_normalize_since(since),
        )
        params = {
            **filter_params,
            "inner_limit": max(1, int(inner_limit)),
            "vec": vl,
            # 中文 bigram / ASCII 词拼成 OR tsquery；空查询用哨兵词避免空 tsquery
            "q_ts": query_tsquery(query),
            "limit": limit,
        }
        t1 = time.monotonic()
        # 复用只读池：一轮对话里 RAG 漏斗和知识库工具会各触发一次检索
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        if timings is not None:
            timings["sql_ms"] = int((time.monotonic() - t1) * 1000)
        docs: list[Document] = []
        for cid, content, title, source_path, doc_id, stock_codes, score, similarity in rows:
            docs.append(Document(
                page_content=content,
                metadata={
                    "id": cid, "title": title, "source_path": source_path,
                    # doc_id 用于「同一文档最多进几块」：年报几百块，按标题限流会误伤
                    "doc_id": str(doc_id) if doc_id is not None else "",
                    # 命中闸的实体一致性校验用；文件类文档为空列表
                    "stock_codes": list(stock_codes) if stock_codes else [],
                    "score": float(score),
                    # 余弦相似度（1 - 余弦距离），与 RAG_THRESHOLD 同尺度，用于相关性判定
                    "similarity": float(similarity) if similarity is not None else 0.0,
                },
            ))
        if RETRIEVAL_MODE == "doc_first":
            docs = _doc_first_select(docs, limit)
        # 重排：RRF 只保证"两路都命中"，不保证真相关；交叉编码器直接判 query↔正文
        if RERANK_ENABLED and docs:
            t2 = time.monotonic()
            try:
                scores = await rerank_scores(query, [d.page_content for d in docs])
                for d, s in zip(docs, scores, strict=True):
                    d.metadata["rerank_score"] = s
            except Exception as e:  # noqa: BLE001 - 重排是增强项，挂了不能拖垮检索
                logger.warning("重排失败，回退 RRF 排序: %s", e)
            if timings is not None:
                timings["rerank_ms"] = int((time.monotonic() - t2) * 1000)
        return docs

    async def _run_search(
        self, query: str, timings: dict | None = None, **filters
    ) -> list[Document]:
        docs = await self._run_candidates(query, timings, **filters)
        # 重排关闭时全为 0 分，stable sort 保持 RRF 序，与旧行为一致
        docs.sort(key=lambda d: d.metadata.get("rerank_score", 0.0), reverse=True)
        docs = _apply_rerank_filter(docs)
        # 融合结果后处理：去噪、去重、控多样性（详见 _clean_docs）
        return _clean_docs(docs, per_doc=self.per_doc)[: self.k]

    async def asearch(
        self,
        query: str,
        *,
        stock_code: str = "",
        stock_codes=None,
        source_type: str = "",
        since: str = "",
        timings: dict | None = None,
    ) -> list[Document]:
        """带过滤的检索入口（工具层用）；非法过滤值会被忽略而不是报错。

        stock_codes 支持代码列表（问题同时点名多家公司时按 OR 过滤）；
        timings 传入时回填 embed_ms / sql_ms / rerank_ms，供图节点做阶段计时。
        """
        return await self._run_search(
            query, timings=timings,
            stock_code=stock_code, stock_codes=stock_codes,
            source_type=source_type, since=since,
        )

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        # 检索本质是异步 IO（httpx + psycopg 池），同步入口只在没有运行中事件循环时桥接；
        # 在图里 asyncio.run 会直接抛 "cannot be called from a running event loop"
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._run_search(query))
        raise RuntimeError("HybridRetriever 只支持异步调用，请使用 ainvoke()/asearch()")

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        return await self._run_search(query)


_WS_RE = re.compile(r"\s+")


def _doc_first_select(docs: list[Document], limit: int) -> list[Document]:
    """按文档聚合 RRF 排名后展开块，返回最多 limit 个候选。

    chunk 模式下同一篇长研报的多块可能占满前 30，把目标文档挤到 31 名之外；
    这里先按「文档」排序（取文档内最高 RRF 的块作为文档名次），再展开每篇的前
    DOC_FIRST_PER_DOC 块，最后用后续文档和剩余块补足 limit。

    注意：这是「文档级聚合」，不是简单的 per-doc 限流——离线重放里按块数硬限流
    会把 Recall@30 压低，必须靠文档级顺序把目标文档先选进来。
    """
    if not docs or limit <= 0:
        return []
    groups: dict = {}
    order: list = []
    for doc in docs:
        key = doc.metadata.get("doc_id") or doc.metadata.get("title") or doc.metadata.get("id")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(doc)

    per_doc = max(1, DOC_FIRST_PER_DOC)
    max_docs = max(1, DOC_FIRST_DOCS)
    selected: list[Document] = []

    def take(keys) -> None:
        for key in keys:
            selected.extend(groups[key][:per_doc])
            if len(selected) >= limit:
                return

    take(order[:max_docs])
    if len(selected) < limit:
        take(order[max_docs:])
    if len(selected) < limit:
        # 仍不足：补齐已选文档里还没进来的块（保持原 RRF 序）
        taken = {id(d) for d in selected}
        for doc in docs:
            if id(doc) not in taken:
                selected.append(doc)
                if len(selected) >= limit:
                    break
    return selected[:limit]


def _content_key(text: str) -> str:
    """去重用的正文键：空白折叠后取全文。

    不能退回前缀比对——ingest 给每个 chunk 头部注入了「[标题路径]」，同标题下的
    兄弟片段前 40 字往往一模一样，按前缀去重会把不同段落整条误删（等于 per_doc=1）。
    只折叠空白不删除空白：宁可漏合并，也不要把两段不同正文判成同一段。
    """
    return _WS_RE.sub(" ", text or "").strip()


def _clean_docs(
    docs: list[Document], sim_floor: float = 0.05, per_doc: int = RAG_PER_DOC
) -> list[Document]:
    """对融合后的候选片段做轻量后处理，提升送入生成的质量。

    - 丢弃余弦相似度过低（< sim_floor）的噪声片段，避免无关资料污染回答；
    - 按「文档 + 归一化全文」去重：SQL 的 fused 已按 id 分组，这里只兜住
      不同 id 承载同一段正文的情况，正常片段一条都不该被删；
    - 同一文档最多保留 per_doc 条。按 doc_id 而不是标题：年报、公告的标题
      在库里高度重复（如「2025年年度报告」），按标题限流等于把整篇年报压到 2 块。
      旧数据没有 doc_id 时回落到标题。
    不改变原有按 RRF 分数的排序，sufficiency_check 仍按最高相似度与数量判定。
    """
    seen_ids: set = set()
    seen_text: set = set()
    per_doc_count: dict = {}
    out: list[Document] = []
    for d in docs:
        sim = d.metadata.get("similarity", 0.0) or 0.0
        if sim < sim_floor:
            continue
        cid = d.metadata.get("id")
        if cid is not None:
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
        doc_key = d.metadata.get("doc_id") or d.metadata.get("title", "")
        key = (doc_key, _content_key(d.page_content))
        if key in seen_text:
            continue
        seen_text.add(key)
        if per_doc_count.get(doc_key, 0) >= per_doc:
            continue
        per_doc_count[doc_key] = per_doc_count.get(doc_key, 0) + 1
        out.append(d)
    return out


def _apply_rerank_filter(docs: list[Document], threshold: float = None,
                         enabled: bool = None) -> list[Document]:
    """按重排分再裁一次上下文（RAG_RERANK_FILTER 开关）。

    命中闸只看最高分，过线后低分片段照样进 prompt 和 sources；开启后
    只有 >= 阈值的片段进上下文。重排分缺失（重排关闭/失败）时原样返回，
    否则会把整批候选裁空。enabled 显式传入时覆盖开关，供离线重放用。
    """
    on = RAG_RERANK_FILTER if enabled is None else enabled
    if not on:
        return docs
    if not any(d.metadata.get("rerank_score") is not None for d in docs):
        return docs
    th = RERANK_THRESHOLD if threshold is None else threshold
    return [d for d in docs if (d.metadata.get("rerank_score") or 0.0) >= th]


def build_retriever(k: int = 6, candidates: int = None, per_doc: int = None) -> HybridRetriever:
    kwargs = {"k": k}
    if candidates is not None:
        kwargs["candidates"] = candidates
    if per_doc is not None:
        kwargs["per_doc"] = per_doc
    return HybridRetriever(**kwargs)
