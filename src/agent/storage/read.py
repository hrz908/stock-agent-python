"""业务主数据只读访问（stock_basic / v_stock_profile / stock_industry）。

与 checkpointer / 会话 / 审计的写连接分开：这里的连接带
`default_transaction_read_only=on`，只服务固定 SQL 的查询工具。
不单独建数据库角色——LLM 不持有凭据、也没有 SQL 通道，这一层挡的是「代码写错」。

连接失败一律抛出，由调用方（工具层）降级成 {"ok": False}，不影响取数与对话。
"""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent.config import DATABASE_URL, DB_RO_POOL_MAX, DB_RO_TIMEOUT
from agent.storage.pool import LazyPool

_pool = LazyPool(
    "主数据只读",
    DATABASE_URL,
    DB_RO_POOL_MAX,
    DB_RO_TIMEOUT,
    kwargs={
        # 只读语句不需要事务，autocommit 省一次 BEGIN/COMMIT
        "autocommit": True,
        # ef_search 默认 40 < 检索 SQL 里的 LIMIT 50，ANN 最多只回 40 条；
        # 带 stock_code/source_type 过滤时 HNSW 还会提前终止，
        # iterative_scan 让它在过滤后继续扫，直到凑够 LIMIT
        # （pgvector <0.8 不认识该项，作为未知 GUC 被忽略）
        "options": (
            "-c default_transaction_read_only=on "
            "-c hnsw.ef_search=100 "
            "-c hnsw.iterative_scan=relaxed_order"
        ),
        "connect_timeout": 3,
    },
)


async def get_read_pool() -> AsyncConnectionPool:
    """惰性建池（进程级单例）。"""
    return await _pool.get()


async def fetch_all(sql: str, params: dict | None = None) -> list[dict]:
    """执行固定 SQL 并返回 dict 行；SQL 只能是本模块常量，参数走命名占位符。"""
    pool = await get_read_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params or {})
            return await cur.fetchall()


async def close_read_pool() -> None:
    """进程退出时释放连接池（lifespan 收尾调用）。"""
    await _pool.close()
