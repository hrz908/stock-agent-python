"""行情 / 搜索 / 主数据接口。

三个对外契约细节必须保持：
- 搜索结果**永远是非 nil 数组**（前端 map/foreach 不判空）；
- 缓存里存的字节与真实响应**逐字节一致**（同一份 JSONBytes 既写缓存也返回）；
- 空搜索结果**不写缓存**：空往往来自第三方临时抽风，缓存住会让搜索一小时全空。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from server.deps import runtime
from server.domain.stockcode import normalize_code
from server.quote.kline import fetch_bars, period_supported
from server.quote.search import SearchResult, search as upstream_search
from server.resp import (
    CODE_BAD_GATEWAY,
    CODE_BAD_REQUEST,
    CODE_INTERNAL,
    CODE_NOT_FOUND,
    CODE_OK,
    ApiError,
    json_bytes,
    ok_response,
)
from server.util.amount import money

logger = logging.getLogger("stocka.server.stock")

router = APIRouter(tags=["stock"])

SEARCH_CACHE_TTL = 3600
QUOTE_CACHE_TTL = 30
KLINE_CACHE_TTL = 30
PROFILE_CACHE_TTL = 3600
MAX_LIMIT = 10
MAX_SEARCH_RUNES = 32
KLINE_DEFAULT_LIMIT = 60
KLINE_MAX_LIMIT = 120

_LIKE_ESCAPES = {"\\": "\\\\", "%": "\\%", "_": "\\_"}


def _like_escape(text: str) -> str:
    out = []
    for ch in text:
        out.append(_LIKE_ESCAPES.get(ch, ch))
    return "".join(out)


def _digits_of(text: str) -> str:
    """抽出输入里的数字（最多 6 位）：支持 "600519" / "SH600519" / "600519.SH" / "600"。"""
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return digits[:6]


def _split_concepts(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _cached_response(payload: bytes, cache: str, source: str | None = None) -> Response:
    headers = {"X-Cache": cache}
    if source:
        headers["X-Search-Source"] = source
    return Response(
        content=payload, media_type="application/json; charset=utf-8", headers=headers
    )


async def _cache_get(key: str) -> bytes | None:
    try:
        raw = await runtime().redis.get(key)
    except Exception:  # noqa: BLE001 - 缓存是旁路
        return None
    if not raw:
        return None
    return raw.encode("utf-8")


async def _cache_set(key: str, payload: bytes, ttl: int) -> None:
    try:
        await runtime().redis.set(key, payload, ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.info("cache set failed (%s): %s", key, exc)


# ---------------------------------------------------------------- 搜索


async def search_local(query: str, limit: int) -> list[SearchResult]:
    """腾讯 smartbox 挂掉或返回空时用主数据表兜底。

    表未建（迁移未执行）只记日志返回空，不把主流程拖失败。
    """
    term = (query or "").strip()
    if not term or limit <= 0:
        return []

    conditions = ["status = 'listed'"]
    args: list[Any] = []
    digits = _digits_of(term)
    if digits:
        conditions.append("code LIKE %s")
        args.append(digits + "%")
    pattern = "%" + _like_escape(term) + "%"
    conditions.append("(name ILIKE %s OR pinyin_abbr ILIKE %s)")
    args.extend([pattern, pattern])

    # 排序：同名优先 → 三元组相似度 → 代码兜底，保证稳定
    sql = (
        "SELECT code, market, name FROM stock_basic WHERE "
        + " AND ".join(conditions)
        + " ORDER BY (name = %s) DESC, similarity(name, %s) DESC, code LIMIT %s"
    )
    args.extend([term, term, limit])

    try:
        async with runtime().pg.pool.connection() as conn:
            cursor = await conn.execute(sql, args)
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 主数据不可用时不影响搜索主路径
        logger.info("local stock search failed: q=%s err=%s", query, exc)
        return []
    return [
        SearchResult(code=row[0], market=row[1], name=row[2] or row[0]) for row in rows
    ]


async def service_search(keyword: str, limit: int) -> list[dict[str, str]]:
    """供 agent 工具层直调（等价于原 HTTP 版 /api/v1/stocks/search 的 data）。"""
    results = await _do_search(keyword, limit)
    return [{"code": r.code, "market": r.market, "name": r.name} for r in results]


async def _do_search(keyword: str, limit: int) -> list[SearchResult]:
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    if len(keyword) > MAX_SEARCH_RUNES:
        raise ApiError(CODE_BAD_REQUEST, "搜索词过长")
    limit = 5 if limit <= 0 else min(limit, MAX_LIMIT)

    results = await upstream_search(keyword, limit)
    if not results:
        results = await search_local(keyword, limit)
    return results


@router.get("/stocks/search")
async def search_stock(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=5),
):
    keyword = (q or "").strip()
    if not keyword:
        return ok_response([])
    if len(keyword) > MAX_SEARCH_RUNES:
        raise ApiError(CODE_BAD_REQUEST, "搜索词过长")

    limit = 5 if limit <= 0 else min(limit, MAX_LIMIT)
    cache_key = f"stock:search:{keyword}:{limit}"

    cached = await _cache_get(cache_key)
    if cached:
        return _cached_response(cached, "HIT")

    results = await upstream_search(keyword, limit)
    source = "tencent"
    if not results:
        results = await search_local(keyword, limit)
        source = "db"
    if not results:
        source = "empty"

    payload = [
        {"code": r.code, "market": r.market, "name": r.name} for r in results
    ]
    # 序列化与缓存写同一份字节，保证缓存内容与真实响应逐字节一致
    raw = json_bytes(CODE_OK, "ok", payload)

    # 空结果不写缓存：空往往来自第三方临时抽风或超时
    if payload:
        await _cache_set(cache_key, raw, SEARCH_CACHE_TTL)

    return _cached_response(raw, "MISS", source)


# ---------------------------------------------------------------- 行情详情


def _detail_payload(detail: Any) -> dict[str, Any]:
    """QuoteDetail → 对外 JSON。

    金额 / 价格**一律字符串**：中间任何一步走 float 都会丢精度，
    前端也按字符串直接展示。
    """
    return {
        "code": detail.code,
        "name": detail.name,
        "price": money(detail.price),
        "change": money(detail.change),
        "change_percent": money(detail.change_percent),
        "open": money(detail.open),
        "high": money(detail.high),
        "low": money(detail.low),
        "volume": detail.volume,
        "amount": money(detail.amount),
        "turnover": money(detail.turnover),
        "volume_ratio": money(detail.volume_ratio),
        "pe": money(detail.pe),
        "pb": money(detail.pb),
        "total_mv": money(detail.total_mv),
        "free_mv": money(detail.free_mv),
        "status": detail.status,
        "time": detail.time,
    }


async def service_quote(code: str) -> dict[str, Any]:
    """供 agent 工具层直调（等价于原 HTTP 版 /api/v1/stock/quote 的 data）。"""
    normalized, ok = normalize_code(code)
    if not ok:
        raise ApiError(
            CODE_BAD_REQUEST, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"
        )
    cache_key = f"stock:quote:{normalized}"
    cached = await _cache_get(cache_key)
    if cached:
        envelope = json.loads(cached.decode("utf-8"))
        return envelope.get("data") or {}

    detail = await runtime().service("market_source").fetch_detail(normalized)
    if detail is None:
        raise ApiError(CODE_BAD_GATEWAY, "行情获取失败，请稍后重试")
    payload = _detail_payload(detail)
    try:
        await _cache_set(cache_key, json_bytes(CODE_OK, "ok", payload), QUOTE_CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return payload


@router.get("/stock/quote")
async def get_quote(request: Request, code: str = Query(default="")):
    normalized, ok = normalize_code(code)
    if not ok:
        raise ApiError(
            CODE_BAD_REQUEST, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"
        )

    cache_key = f"stock:quote:{normalized}"
    cached = await _cache_get(cache_key)
    if cached:
        return _cached_response(cached, "HIT")

    detail = await runtime().service("market_source").fetch_detail(normalized)
    if detail is None:
        logger.info("fetch quote detail failed: code=%s", normalized)
        raise ApiError(CODE_BAD_GATEWAY, "行情获取失败，请稍后重试")

    raw = json_bytes(CODE_OK, "ok", _detail_payload(detail))
    await _cache_set(cache_key, raw, QUOTE_CACHE_TTL)
    return _cached_response(raw, "MISS")


# ---------------------------------------------------------------- K 线


@router.get("/stock/kline")
async def get_kline(
    request: Request,
    code: str = Query(default=""),
    period: str = Query(default="day"),
    limit: int = Query(default=KLINE_DEFAULT_LIMIT),
):
    normalized, ok = normalize_code(code)
    if not ok:
        raise ApiError(
            CODE_BAD_REQUEST, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"
        )

    period = (period or "").strip() or "day"
    if not period_supported(period):
        raise ApiError(CODE_BAD_REQUEST, "周期只支持 m1 / m5 / m15 / day / week")

    limit = KLINE_DEFAULT_LIMIT if limit <= 0 else min(limit, KLINE_MAX_LIMIT)
    cache_key = f"stock:kline:{normalized}:{period}:{limit}"

    cached = await _cache_get(cache_key)
    if cached:
        return _cached_response(cached, "HIT")

    try:
        bars = await fetch_bars(normalized, period, limit)
    except Exception as exc:  # noqa: BLE001
        logger.info("fetch kline failed: code=%s period=%s err=%s", normalized, period, exc)
        raise ApiError(CODE_BAD_GATEWAY, "K线获取失败，请稍后重试") from exc
    if not bars:
        raise ApiError(CODE_BAD_GATEWAY, "K线获取失败，请稍后重试")

    raw = json_bytes(
        CODE_OK,
        "ok",
        {
            "code": normalized,
            "period": period,
            "bars": [
                {"d": b.d, "o": str(b.o), "h": str(b.h), "l": str(b.l), "c": str(b.c), "v": b.v}
                for b in bars
            ],
        },
    )
    await _cache_set(cache_key, raw, KLINE_CACHE_TTL)
    return _cached_response(raw, "MISS")


# ---------------------------------------------------------------- 主数据

_PROFILE_SQL = """
SELECT code, market, ts_code, gw_code, name, board, status, is_st,
       COALESCE(industry_l1, '') AS industry_l1,
       COALESCE(industry_l2, '') AS industry_l2,
       COALESCE(industry_l3, '') AS industry_l3,
       COALESCE(array_to_string(concepts, ','), '') AS concepts,
       COALESCE(to_char(list_date, 'YYYY-MM-DD'), '') AS list_date,
       COALESCE(to_char(delist_date, 'YYYY-MM-DD'), '') AS delist_date
FROM v_stock_profile
WHERE code = %s
"""


async def fetch_profile(digits: str) -> dict[str, Any] | None:
    """按 6 位裸码查主数据；None 表示库里没有这只股票。"""
    try:
        async with runtime().pg.pool.connection() as conn:
            cursor = await conn.execute(_PROFILE_SQL, (digits,))
            row = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.info("fetch stock profile failed: code=%s err=%s", digits, exc)
        raise ApiError(CODE_INTERNAL, "主数据查询失败，请稍后重试") from exc
    if row is None or not row[0]:
        return None

    profile = {
        "code": row[0],
        "market": row[1],
        "ts_code": row[2],
        "gw_code": row[3],
        "name": row[4],
        "board": row[5],
        "status": row[6],
        "is_st": bool(row[7]),
        "industry_l1": row[8] or "",
        "industry_l2": row[9] or "",
        "industry_l3": row[10] or "",
        "concepts": _split_concepts(row[11]),
        "list_date": row[12] or "",
        "delist_date": row[13] or "",
    }
    # omitempty：空值不下发，前端有兜底
    return {k: v for k, v in profile.items() if not (k.endswith("_date") and v == "")}


@router.get("/stocks/profile")
async def get_profile(request: Request, code: str = Query(default="")):
    normalized, ok = normalize_code(code)
    if not ok:
        raise ApiError(
            CODE_BAD_REQUEST, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"
        )
    digits = normalized[2:]
    cache_key = f"stock:profile:{digits}"

    cached = await _cache_get(cache_key)
    if cached:
        return _cached_response(cached, "HIT")

    profile = await fetch_profile(digits)
    if profile is None:
        raise ApiError(
            CODE_NOT_FOUND, "没有该股票的主数据，请先执行 sync-master"
        )

    raw = json_bytes(CODE_OK, "ok", profile)
    await _cache_set(cache_key, raw, PROFILE_CACHE_TTL)
    return _cached_response(raw, "MISS")
