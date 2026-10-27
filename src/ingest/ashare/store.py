"""pgvector 写入与增量状态。

事务边界是「一份文档」：documents + chunks + ashare_documents 同进同退。
中途失败宁可整份回滚重跑，也不要留下「有文档行没有 chunk」的空壳。
"""
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import psycopg
import psycopg.errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ingest.ashare import config
from ingest.ashare.quality import from_signed64, hamming, to_signed64
from ingest.ashare.types import Chunk, RunStats
from shared.zh_tokenize import document_text

logger = logging.getLogger(__name__)

_DOC_COLUMNS = (
    "doc_id, source, source_id, source_type, title, url, pub_time, stock_codes, "
    "stock_names, industry, org, author, rating, event_type, quality_score, "
    "fingerprint, simhash, simhash_bands, content_bytes, source_hash, extra"
)


def vec_literal(embedding: Iterable[float]) -> str:
    return "[" + ",".join(f"{float(x):.8f}" for x in embedding) + "]"


class Store:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or config.DATABASE_URL
        self.conn: psycopg.Connection | None = None

    # ------------------------------------------------------------------ 基础
    def connect(self) -> None:
        self.conn = psycopg.connect(self.dsn, row_factory=dict_row)
        # 批量入库时 WAL 同步不是瓶颈；崩溃最多丢一批可重跑的数据
        self.conn.execute("SET synchronous_commit = off")
        # 检索入口同样受 HNSW 默认 ef_search=40 限制（CLI 里 LIMIT 8，影响有限，
        # 但带过滤时提前终止会让结果变少）；pgvector <0.8 时 iterative_scan 是空操作
        self.conn.execute("SET hnsw.ef_search = 100")
        self.conn.execute("SET hnsw.iterative_scan = relaxed_order")
        self.conn.commit()

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def ensure_schema(self) -> None:
        tables = ("documents", "chunks", "ashare_documents", "ashare_sync_state", "ashare_ingest_runs")
        for table in tables:
            try:
                self.conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
                self.conn.commit()
            except psycopg.errors.UndefinedTable as exc:
                self.conn.rollback()
                raise RuntimeError(
                    f"缺少表 {table}：先执行 sql/migrations/007_ashare_corpus.sql"
                ) from exc
        logger.info("数据库结构校验通过")

    # ------------------------------------------------------------------ 查询
    def get_watermark(self, source: str, scope: str = "") -> datetime | None:
        row = self.conn.execute(
            "SELECT watermark FROM ashare_sync_state WHERE source = %s AND scope = %s",
            (source, scope),
        ).fetchone()
        self.conn.commit()
        return row["watermark"] if row else None

    def state_for(self, source: str, source_id: str) -> dict | None:
        row = self.conn.execute(
            """
            SELECT a.source_hash, a.fingerprint, a.pub_time,
                   d.status, d.embed_model, d.content_hash, d.chunk_count
            FROM ashare_documents a
            JOIN documents d ON d.id = a.doc_id
            WHERE a.source = %s AND a.source_id = %s
            """,
            (source, source_id),
        ).fetchone()
        self.conn.commit()
        return dict(row) if row else None

    def fingerprint_exists(self, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM ashare_documents WHERE fingerprint = %s LIMIT 1", (fingerprint,)
        ).fetchone()
        self.conn.commit()
        return row is not None

    def find_near_dup(
        self,
        simhash: int,
        bands: list[int],
        content_bytes: int,
        source_type: str,
        max_hamming: int | None = None,
    ) -> str | None:
        """候选召回（共享任一 16bit 段）+ 汉明距离与长度比双条件确认。

        只看同 source_type：快讯与年报的模板句偶有重合，跨类型比对纯属浪费。
        """
        if not bands:
            return None
        limit = max_hamming if max_hamming is not None else config.NEAR_DUP_HAMMING
        rows = self.conn.execute(
            """
            SELECT fingerprint, simhash, content_bytes
            FROM ashare_documents
            WHERE simhash_bands && %s::int[] AND source_type = %s
            LIMIT 80
            """,
            (bands, source_type),
        ).fetchall()
        self.conn.commit()

        for row in rows:
            if content_bytes and row["content_bytes"]:
                ratio = row["content_bytes"] / content_bytes
                if ratio < 0.75 or ratio > 1.33:
                    continue
            if hamming(from_signed64(row["simhash"]), from_signed64(simhash)) <= limit:
                return row["fingerprint"]
        return None

    # ------------------------------------------------------------------ 写入
    def upsert_document(
        self,
        *,
        source_path: str,
        title: str,
        mime_type: str,
        content_hash: str,
        embed_model: str,
        meta: dict[str, Any],
        chunks: list[Chunk],
    ) -> str | None:
        """写入一份文档。返回 doc_id；指纹撞库（跨源重复）返回 None。"""
        try:
            cur = self.conn.execute(
                """
                INSERT INTO documents
                    (source_path, title, mime_type, content_hash, embed_model,
                     status, chunk_count, error, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'ok', %s, NULL, now())
                ON CONFLICT (source_path) DO UPDATE SET
                    title = EXCLUDED.title,
                    mime_type = EXCLUDED.mime_type,
                    content_hash = EXCLUDED.content_hash,
                    embed_model = EXCLUDED.embed_model,
                    status = 'ok',
                    chunk_count = EXCLUDED.chunk_count,
                    error = NULL,
                    updated_at = now()
                RETURNING id
                """,
                (source_path, title, mime_type, content_hash, embed_model, len(chunks)),
            )
            doc_id = cur.fetchone()["id"]

            self.conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
            self._insert_chunks(doc_id, chunks)
            self.conn.execute(
                f"""
                INSERT INTO ashare_documents ({_DOC_COLUMNS}, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s, %s, %s, %s,
                        %s, %s, %s, %s, %s::int[], %s, %s, %s::jsonb, now())
                ON CONFLICT (source, source_id) DO UPDATE SET
                    doc_id = EXCLUDED.doc_id,
                    source_type = EXCLUDED.source_type,
                    title = EXCLUDED.title,
                    url = EXCLUDED.url,
                    pub_time = EXCLUDED.pub_time,
                    stock_codes = EXCLUDED.stock_codes,
                    stock_names = EXCLUDED.stock_names,
                    industry = EXCLUDED.industry,
                    org = EXCLUDED.org,
                    author = EXCLUDED.author,
                    rating = EXCLUDED.rating,
                    event_type = EXCLUDED.event_type,
                    quality_score = EXCLUDED.quality_score,
                    fingerprint = EXCLUDED.fingerprint,
                    simhash = EXCLUDED.simhash,
                    simhash_bands = EXCLUDED.simhash_bands,
                    content_bytes = EXCLUDED.content_bytes,
                    source_hash = EXCLUDED.source_hash,
                    extra = EXCLUDED.extra,
                    fetched_at = now()
                """,
                (
                    doc_id,
                    meta["source"], meta["source_id"], meta["source_type"], meta["title"],
                    meta.get("url"), meta.get("pub_time"), meta.get("stock_codes") or [],
                    meta.get("stock_names") or [], meta.get("industry"), meta.get("org"),
                    meta.get("author"), meta.get("rating"), meta.get("event_type"),
                    meta.get("quality_score", 0), meta["fingerprint"],
                    to_signed64(meta["simhash"]), meta.get("simhash_bands") or [],
                    meta.get("content_bytes", 0), meta.get("source_hash", ""),
                    Jsonb(meta.get("extra") or {}),
                ),
            )
            self.conn.commit()
            return str(doc_id)
        except psycopg.errors.UniqueViolation as exc:
            self.conn.rollback()
            if "fingerprint" in str(exc):
                logger.info("内容指纹重复，跳过：%s", title[:40])
                return None
            raise
        except Exception:
            self.conn.rollback()
            raise

    def _insert_chunks(self, doc_id, chunks: list[Chunk]) -> None:
        rows = [
            (
                doc_id,
                chunk.seq,
                chunk.content,
                document_text(chunk.content),
                chunk.token_count,
                Jsonb({**chunk.metadata, "doc_id": str(doc_id)}),
                vec_literal(chunk.embedding),
            )
            for chunk in chunks
        ]
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO chunks (document_id, seq, content, search_text,
                                    token_count, metadata, embedding)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::vector)
                """,
                rows,
            )

    # ------------------------------------------------------------------ 运行记录
    def start_run(self, source: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO ashare_ingest_runs (source) VALUES (%s) RETURNING id", (source,)
        )
        run_id = cur.fetchone()["id"]
        self.conn.commit()
        return int(run_id)

    def finish_run(self, run_id: int, stats: RunStats, error: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE ashare_ingest_runs
            SET finished_at = now(), fetched = %s, written = %s, skipped = %s,
                failed = %s, chunks = %s, error = %s
            WHERE id = %s
            """,
            (stats.fetched, stats.written, stats.skipped, stats.failed, stats.chunks, error, run_id),
        )
        self.conn.commit()

    def set_watermark(self, source: str, watermark: datetime | None, *, ok: bool, error: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO ashare_sync_state (source, scope, watermark, last_run_at, last_ok_at, error)
            VALUES (%s, '', %s, now(), CASE WHEN %s THEN now() ELSE NULL END, %s)
            ON CONFLICT (source, scope) DO UPDATE SET
                watermark = COALESCE(EXCLUDED.watermark, ashare_sync_state.watermark),
                last_run_at = now(),
                last_ok_at = CASE WHEN %s THEN now() ELSE ashare_sync_state.last_ok_at END,
                error = EXCLUDED.error
            """,
            (source, watermark, ok, error, ok),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ 统计/重嵌/检索
    def storage_report(self) -> dict[str, Any]:
        """表/索引实际占用 + 每块均值，用于判断要不要换 halfvec 或降维。"""
        tables = self.conn.execute(
            """
            SELECT c.relname AS name, pg_total_relation_size(c.oid) AS bytes
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relkind = 'r'
              AND c.relname = ANY(%s::text[])
            ORDER BY bytes DESC
            """,
            (["chunks", "documents", "ashare_documents", "ashare_ingest_runs", "ashare_sync_state"],),
        ).fetchall()
        indexes = self.conn.execute(
            """
            SELECT i.indexrelname AS name, pg_relation_size(i.indexrelid) AS bytes
            FROM pg_stat_user_indexes i
            WHERE i.relname IN ('chunks', 'ashare_documents')
            ORDER BY bytes DESC
            """
        ).fetchall()
        counts = self.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id
               WHERE starts_with(d.source_path, 'ashare://')) AS chunks,
              (SELECT count(*) FROM chunks) AS total_chunks,
              (SELECT count(*) FROM ashare_documents) AS docs
            """
        ).fetchone()
        self.conn.commit()
        return {
            "tables": [dict(r) for r in tables],
            "indexes": [dict(r) for r in indexes],
            "chunks": int(counts["chunks"] or 0),
            "total_chunks": int(counts["total_chunks"] or 0),
            "docs": int(counts["docs"] or 0),
            "dim": config.EMBED_DIM,
        }

    def stats(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT * FROM ashare_corpus_stats ORDER BY source, source_type"
        ).fetchall()
        total = self.conn.execute(
            "SELECT count(*) AS docs FROM ashare_documents"
        ).fetchone()
        chunks = self.conn.execute(
            """
            SELECT count(*) AS chunks FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE starts_with(d.source_path, 'ashare://')
            """
        ).fetchone()
        self.conn.commit()
        return {"rows": [dict(r) for r in rows], "docs": total["docs"], "chunks": chunks["chunks"]}

    def stale_doc_ids(self, embed_model: str, limit: int | None = None) -> list[str]:
        sql = """
            SELECT a.doc_id
            FROM ashare_documents a
            JOIN documents d ON d.id = a.doc_id
            WHERE d.embed_model <> %s
            ORDER BY a.pub_time DESC NULLS LAST
        """
        params: list[Any] = [embed_model]
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        self.conn.commit()
        return [str(r["doc_id"]) for r in rows]

    def load_chunks(self, doc_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, content FROM chunks WHERE document_id = %s ORDER BY seq", (doc_id,)
        ).fetchall()
        self.conn.commit()
        return [dict(r) for r in rows]

    def update_embeddings(self, rows: list[tuple[int, list[float]]], doc_id: str, embed_model: str) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(
                "UPDATE chunks SET embedding = %s::vector WHERE id = %s",
                [(vec_literal(vec), chunk_id) for chunk_id, vec in rows],
            )
        self.conn.execute(
            "UPDATE documents SET embed_model = %s, updated_at = now() WHERE id = %s",
            (embed_model, doc_id),
        )
        self.conn.commit()

    def search(
        self,
        vector: list[float],
        k: int = 8,
        source_type: str | None = None,
        code: str | None = None,
        since: datetime | None = None,
    ) -> list[dict]:
        clauses = ["TRUE"]
        params: list[Any] = []
        if source_type:
            clauses.append("a.source_type = %s")
            params.append(source_type)
        if code:
            # 用数组重叠而不是 = ANY：前者能走 stock_codes 的 GIN 索引
            clauses.append("a.stock_codes && ARRAY[%s]::text[]")
            params.append(code)
        if since:
            clauses.append("a.pub_time >= %s")
            params.append(since)

        literal = vec_literal(vector)
        sql = f"""
            SELECT c.content, d.title, a.source_type, a.pub_time, a.url, a.stock_codes,
                   1 - (c.embedding <=> %s::vector) AS similarity
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            JOIN ashare_documents a ON a.doc_id = d.id
            WHERE {' AND '.join(clauses)}
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
        """
        rows = self.conn.execute(sql, [literal, *params, literal, k]).fetchall()
        self.conn.commit()
        return [dict(r) for r in rows]
