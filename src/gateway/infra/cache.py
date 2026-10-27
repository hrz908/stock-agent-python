"""Redis 字节缓存（旁路）。

行情/搜索/主数据把「已序列化的 JSON 字节」直接存进去：读命中时原样返回，
保证缓存内容与真实响应逐字节一致。缓存不可用一律降级，不影响主链路。
"""

from __future__ import annotations

import logging

from gateway.infra.deps import runtime

logger = logging.getLogger("stocka.gateway.cache")


async def get_bytes(key: str) -> bytes | None:
    try:
        raw = await runtime().redis.get(key)
    except Exception:  # noqa: BLE001 - 缓存是旁路
        return None
    if not raw:
        return None
    return raw.encode("utf-8")


async def set_bytes(key: str, payload: bytes, ttl: int) -> None:
    try:
        await runtime().redis.set(key, payload, ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.info("cache set failed (%s): %s", key, exc)
