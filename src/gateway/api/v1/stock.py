"""行情 / 搜索 / 主数据 HTTP 接口。

这一层负责「字节级缓存」：命中缓存时把存量字节原样吐出，
未命中时用同一份 JSONBytes 既写缓存又作为响应体，保证两者逐字节一致。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import Response

from gateway.infra import cache
from gateway.infra.resp import CODE_OK, json_bytes, ok_response
from gateway.services import stock as service

router = APIRouter(tags=["stock"])


def _cached_response(payload: bytes, cache_state: str, source: str | None = None) -> Response:
    headers = {"X-Cache": cache_state}
    if source:
        headers["X-Search-Source"] = source
    return Response(
        content=payload, media_type="application/json; charset=utf-8", headers=headers
    )


@router.get("/stocks/search")
async def search_stock(
    q: str = Query(default=""),
    limit: int = Query(default=5),
):
    keyword = (q or "").strip()
    if not keyword:
        return ok_response([])
    keyword = service.check_keyword(keyword)

    limit = service.normalize_limit(limit)
    cache_key = f"stock:search:{keyword}:{limit}"

    cached = await cache.get_bytes(cache_key)
    if cached:
        return _cached_response(cached, "HIT")

    payload, source = await service.search_payload(keyword, limit)
    # 序列化与缓存写同一份字节，保证缓存内容与真实响应逐字节一致
    raw = json_bytes(CODE_OK, "ok", payload)

    # 空结果不写缓存：空往往来自第三方临时抽风或超时
    if payload:
        await cache.set_bytes(cache_key, raw, service.SEARCH_CACHE_TTL)

    return _cached_response(raw, "MISS", source)


@router.get("/stock/quote")
async def get_quote(code: str = Query(default="")):
    normalized = service.normalize_stock_code(code)
    cache_key = f"stock:quote:{normalized}"

    cached = await cache.get_bytes(cache_key)
    if cached:
        return _cached_response(cached, "HIT")

    payload = await service.quote_payload(normalized)
    raw = json_bytes(CODE_OK, "ok", payload)
    await cache.set_bytes(cache_key, raw, service.QUOTE_CACHE_TTL)
    return _cached_response(raw, "MISS")


@router.get("/stock/kline")
async def get_kline(
    code: str = Query(default=""),
    period: str = Query(default="day"),
    limit: int = Query(default=service.KLINE_DEFAULT_LIMIT),
):
    normalized, period = service.normalize_stock_code(code), service.normalize_period(period)
    limit = (
        service.KLINE_DEFAULT_LIMIT if limit <= 0 else min(limit, service.KLINE_MAX_LIMIT)
    )
    cache_key = f"stock:kline:{normalized}:{period}:{limit}"

    cached = await cache.get_bytes(cache_key)
    if cached:
        return _cached_response(cached, "HIT")

    _, _, _, payload = await service.kline_payload(normalized, period, limit)
    raw = json_bytes(CODE_OK, "ok", payload)
    await cache.set_bytes(cache_key, raw, service.KLINE_CACHE_TTL)
    return _cached_response(raw, "MISS")


@router.get("/stocks/profile")
async def get_profile(code: str = Query(default="")):
    normalized = service.normalize_stock_code(code)
    digits = normalized[2:]
    cache_key = f"stock:profile:{digits}"

    cached = await cache.get_bytes(cache_key)
    if cached:
        return _cached_response(cached, "HIT")

    _, payload = await service.profile_payload(normalized)
    raw = json_bytes(CODE_OK, "ok", payload)
    await cache.set_bytes(cache_key, raw, service.PROFILE_CACHE_TTL)
    return _cached_response(raw, "MISS")
