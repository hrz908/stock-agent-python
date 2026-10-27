"""数据库操作层：使用 psycopg async 操作 PostgreSQL。"""
import json
import logging

import psycopg
import psycopg.errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from shared.zh_tokenize import document_text

logger = logging.getLogger(__name__)


def _vec_literal(embedding: list) -> str:
    """list[float] -> pgvector 文本字面量 '[v0,v1,...]'（配合 ::vector 强转使用）。"""
    return "[" + ",".join(f"{float(x):.8f}" for x in embedding) + "]"


class Store:
    """文档和 chunks 的数据库操作封装。"""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._pool: AsyncConnectionPool | None = None

    async def connect(self):
        """初始化连接池"""
        self._pool = AsyncConnectionPool(
            conninfo=self.database_url,
            min_size=2,
            max_size=10,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await self._pool.open()
        logger.info("数据库连接池已建立")

    async def close(self):
        """关闭连接池"""
        if self._pool:
            await self._pool.close()
            logger.info("数据库连接池已关闭")

    async def ensure_tables(self):
        """
        校验 documents 和 chunks 表已存在。

        表结构由 sql/migrations/005_agent.sql 管理，ingest 不自建表，
        这里只做启动前置检查，缺表时快速失败并给出提示。
        """
        try:
            async with self._pool.connection() as conn:
                await conn.execute("SELECT 1 FROM documents LIMIT 1")
                await conn.execute("SELECT 1 FROM chunks LIMIT 1")
        except psycopg.errors.UndefinedTable as e:
            raise RuntimeError(
                "数据库表不存在，请先执行 sql/migrations/005_agent.sql 迁移"
            ) from e
        logger.info("数据库表校验通过（schema 由 migration 005 管理）")

    async def delete_document_by_path(self, source_path: str):
        """
        文件被删除时，级联删除 document。
        chunks 通过 ON DELETE CASCADE 自动删除。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM documents WHERE source_path = %s RETURNING id",
                (source_path,),
            )
            row = await cur.fetchone()
            await conn.commit()
            if row:
                logger.info("已删除文档及其 chunks: %s (id=%s)", source_path, row["id"])
            else:
                logger.debug("文档不存在，无需删除: %s", source_path)

    async def get_stale_documents(self, current_model: str) -> list[dict]:
        """
        获取 embed_model != current_model 的文档，用于 reembed。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, source_path, title, mime_type, content_hash,
                       embed_model, status, chunk_count, error, updated_at
                FROM documents
                WHERE embed_model != %s AND status = 'ok'
                  AND NOT starts_with(source_path, 'ashare://')
                ORDER BY updated_at ASC
                """,
                (current_model,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def transactional_upsert(
        self,
        source_path: str,
        title: str,
        mime_type: str,
        content_hash: str,
        embed_model: str,
        chunks: list[dict],
    ) -> str | None:
        """
        单事务写入：upsert document -> delete old chunks -> insert new chunks。
        
        返回 document_id，如果幂等跳过则返回 None。
        """
        async with self._pool.connection() as conn:
            async with conn.transaction():
                # 1. 查询现有记录判断幂等
                cur = await conn.execute(
                    """
                    SELECT id, content_hash, embed_model, status
                    FROM documents
                    WHERE source_path = %s
                    """,
                    (source_path,),
                )
                row = await cur.fetchone()

                if row:
                    if (
                        row["content_hash"] == content_hash
                        and row["embed_model"] == embed_model
                        and row["status"] == "ok"
                    ):
                        logger.debug("文档无变化，跳过事务写入: %s", source_path)
                        return None

                # upsert（幂等跳过已在上方处理），RETURNING id
                cur = await conn.execute(
                    """
                    INSERT INTO documents
                        (source_path, title, mime_type, content_hash,
                         embed_model, status, chunk_count, error, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'ok', %s, NULL, now())
                    ON CONFLICT (source_path) DO UPDATE SET
                        content_hash = EXCLUDED.content_hash,
                        embed_model  = EXCLUDED.embed_model,
                        title        = EXCLUDED.title,
                        mime_type    = EXCLUDED.mime_type,
                        chunk_count  = EXCLUDED.chunk_count,
                        status       = 'ok',
                        error        = NULL,
                        updated_at   = now()
                    RETURNING id
                    """,
                    (source_path, title, mime_type, content_hash, embed_model, len(chunks)),
                )
                doc_id = str((await cur.fetchone())["id"])

                await conn.execute(
                    "DELETE FROM chunks WHERE document_id = %s",
                    (doc_id,),
                )

                # 批量插入新 chunks（id 自增、tsv 生成列均不写入）
                # psycopg3 的 executemany 是游标方法，须经 cursor 调用
                if chunks:
                    rows = [
                        (
                            doc_id,
                            chunk["seq"],
                            chunk["content"],
                            document_text(chunk["content"]),
                            chunk.get("token_count"),
                            json.dumps(chunk.get("metadata", {}), ensure_ascii=False),
                            _vec_literal(chunk["embedding"]),
                        )
                        for chunk in chunks
                    ]
                    async with conn.cursor() as cur:
                        await cur.executemany(
                            """
                            INSERT INTO chunks (document_id, seq, content, search_text,
                                                token_count, metadata, embedding)
                            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::vector)
                            """,
                            rows,
                        )

        logger.info("事务写入完成: %s -> %d chunks", source_path, len(chunks))
        return doc_id

