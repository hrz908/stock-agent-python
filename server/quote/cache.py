"""行情 Redis 缓存（30s 快照）。

批量读走分片 MGET、批量写走 pipeline：自选股上千时单次 MGET 上千个 key
会把 RTT 与内存峰值一起拉高。
"""

from __future__ import annotations

import logging

from server.deps import runtime
from server.quote.source import StockSnapshot
from server.util.unique import chunk

logger = logging.getLogger("stocka.server.quote.cache")

_KEY_PREFIX = "stock:"
_KEY_SUFFIX = ":latest"


def snapshot_key(code: str) -> str:
    return f"{_KEY_PREFIX}{code}{_KEY_SUFFIX}"


async def save_quote(snapshot: StockSnapshot) -> None:
    rt = runtime()
    try:
        await rt.redis.set(
            snapshot_key(snapshot.code),
            _encode(snapshot),
            ex=int(rt.cfg.http.quote_cache_ttl) or 30,
        )
    except Exception as exc:  # noqa: BLE001 - 缓存是旁路
        logger.info("save quote cache failed: %s", exc)


async def batch_save_quotes(snapshots: list[StockSnapshot]) -> None:
    if not snapshots:
        return
    rt = runtime()
    ttl = int(rt.cfg.http.quote_cache_ttl) or 30
    try:
        pipeline = rt.redis.pipeline(transaction=False)
        for snapshot in snapshots:
            if snapshot is None:
                continue
            pipeline.set(snapshot_key(snapshot.code), _encode(snapshot), ex=ttl)
        await pipeline.execute()
    except Exception as exc:  # noqa: BLE001
        logger.info("batch save quote cache failed: %s", exc)


async def get_quote(code: str) -> StockSnapshot | None:
    rt = runtime()
    try:
        raw = await rt.redis.get(snapshot_key(code))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        import json

        return StockSnapshot.from_dict(json.loads(raw))
    except Exception as exc:  # noqa: BLE001
        logger.info("unmarshal quote %s failed: %s", code, exc)
        return None


async def batch_get_quotes(codes: list[str]) -> dict[str, StockSnapshot]:
    """未命中的 code 不出现在结果里，由调用方按降级策略处理。"""
    out: dict[str, StockSnapshot] = {}
    if not codes:
        return out
    rt = runtime()
    size = rt.cfg.http.quote_mget_chunk or 100

    import json

    for group in chunk(codes, size):
        keys = [snapshot_key(code) for code in group]
        try:
            values = await rt.redis.mget(keys)
        except Exception as exc:  # noqa: BLE001
            logger.info("batch get quote cache failed: %s", exc)
            return out
        for code, raw in zip(group, values, strict=False):
            if not raw:
                continue
            try:
                out[code] = StockSnapshot.from_dict(json.loads(raw))
            except Exception:  # noqa: BLE001
                continue
    return out


def _encode(snapshot: StockSnapshot) -> str:
    import json

    return json.dumps(snapshot.to_dict(), ensure_ascii=False, separators=(",", ":"))
