"""连接池的惰性单例骨架。

只做一件事：并发首调只建一个池，失败不缓存半成品，退出时可显式关闭。
两个池（read / app）的差别全部由构造参数表达，避免各写一份 open/close 逻辑。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("stocka.agent")


class LazyPool:
    """进程级惰性连接池。`kwargs` 原样透传给 AsyncConnectionPool。"""

    def __init__(
        self,
        name: str,
        conninfo: str,
        max_size: int,
        open_timeout: float,
        **kwargs: Any,
    ) -> None:
        self._name = name
        self._conninfo = conninfo
        self._max_size = max(1, max_size)
        self._open_timeout = open_timeout
        self._kwargs = kwargs
        self._pool: AsyncConnectionPool | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> AsyncConnectionPool:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                pool = AsyncConnectionPool(
                    conninfo=self._conninfo,
                    min_size=1,
                    max_size=self._max_size,
                    open=False,
                    **self._kwargs,
                )
                try:
                    await pool.open(wait=True, timeout=self._open_timeout)
                except Exception:
                    await pool.close()
                    raise
                self._pool = pool
                logger.info("%s 连接池就绪：max=%d", self._name, self._max_size)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
