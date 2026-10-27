"""Redis 客户端。

与 Go 版 db.NewRedis 对齐：Redis 连不上不阻断启动（降级模式）。
缓存与限流都是旁路逻辑，Redis 故障时按 fail-open 放行，不能因此让整个服务起不来。
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from server.config import Config

logger = logging.getLogger("stocka.server.db")


def _split_addr(addr: str) -> tuple[str, int]:
    text = (addr or "").strip() or "127.0.0.1:6379"
    if text.startswith("redis://") or text.startswith("rediss://"):
        # 允许直接写 URL；交给 from_url 处理
        raise ValueError("URL form is handled separately")
    if ":" in text:
        host, _, port = text.rpartition(":")
        return host, int(port)
    return text, 6379


def create_redis(cfg: Config) -> aioredis.Redis:
    addr = (cfg.redis_addr or "").strip()
    pool_size = cfg.redis.pool_size or 10
    common: dict = {
        "password": cfg.redis_password or None,
        "db": cfg.redis_db,
        "decode_responses": True,
        "socket_connect_timeout": 3.0,
        "socket_timeout": 5.0,
        "max_connections": pool_size,
        "health_check_interval": 30,
    }
    if addr.startswith("redis://") or addr.startswith("rediss://"):
        client = aioredis.from_url(addr, **common)
    else:
        host, port = _split_addr(addr)
        client = aioredis.Redis(host=host, port=port, **common)
    return client


async def ping_redis(client: aioredis.Redis) -> bool:
    """启动探活；失败只告警，不阻断启动（降级模式）。"""
    try:
        await client.ping()
        return True
    except Exception as exc:  # noqa: BLE001 - 降级模式，任何异常都只记日志
        logger.warning("redis unavailable, running in degraded mode: %s", exc)
        return False
