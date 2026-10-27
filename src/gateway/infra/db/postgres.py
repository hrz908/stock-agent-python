"""PostgreSQL 连接池。

与 Go 版 db.NewPostgres 对齐：启动必须探活成功（3s 超时）才继续，
连不上直接终止启动——数据库是强依赖，带病启动只会把错误推到请求期。
"""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

from gateway.infra.config import DBConfig

logger = logging.getLogger("stocka.gateway.db")


class Postgres:
    def __init__(self, dsn: str, cfg: DBConfig) -> None:
        self._dsn = dsn
        self._cfg = cfg
        self._pool: AsyncConnectionPool | None = None

    @property
    def pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("postgres pool is not initialised")
        return self._pool

    async def open(self) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=1,
            max_size=max(1, self._cfg.max_open_conns),
            # psycopg_pool 的 lifetime 单位是秒
            max_lifetime=self._cfg.conn_max_lifetime or None,
            max_idle=self._cfg.conn_max_idle_time or None,
            timeout=3.0,
            open=False,
        )
        await self._pool.open(wait=True, timeout=5.0)
        async with self._pool.connection() as conn:
            await conn.execute("SELECT 1")
        logger.info(
            "postgres connected (max_open_conns=%d)", self._cfg.max_open_conns
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
