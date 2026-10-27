"""应用表读写连接池（chat_sessions / agent_runs）。

与 checkpointer 的池分开（那个只服务 saver），避免会话查询与 checkpoint 写抢连接；
有界、收尾由 lifespan 关闭。
"""
from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from agent.config import APP_POOL_MAX, DATABASE_URL
from agent.storage.pool import LazyPool

_pool = LazyPool("应用表读写", DATABASE_URL, APP_POOL_MAX, 5, kwargs={"connect_timeout": 3})


async def get_app_pool() -> AsyncConnectionPool:
    """惰性建池（进程级单例）。"""
    return await _pool.get()


async def close_app_pool() -> None:
    await _pool.close()
