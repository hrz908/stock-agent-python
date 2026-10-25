"""应用表读写连接池（chat_sessions / agent_runs）。

与 checkpointer 的池分开（那个只服务 saver），避免会话查询与 checkpoint 写抢连接；
有界、收尾由 lifespan 关闭。
"""
import asyncio

from psycopg_pool import AsyncConnectionPool

from config import APP_POOL_MAX, DATABASE_URL

_pool: AsyncConnectionPool | None = None
_lock = asyncio.Lock()


async def get_pool() -> AsyncConnectionPool:
    """惰性建池（进程级单例）；并发首调只建一个，失败不缓存半成品。"""
    global _pool
    if _pool is not None:
        return _pool
    async with _lock:
        if _pool is None:
            pool = AsyncConnectionPool(
                conninfo=DATABASE_URL,
                min_size=1,
                max_size=max(1, APP_POOL_MAX),
                open=False,
                kwargs={"connect_timeout": 3},
            )
            try:
                await pool.open(wait=True, timeout=5)
            except Exception:
                await pool.close()
                raise
            _pool = pool
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
