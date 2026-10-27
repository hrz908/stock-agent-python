"""行情 / 搜索 / 主数据的业务层。

三个对外契约细节必须保持：
- 搜索结果**永远是非 nil 数组**（前端 map/foreach 不判空）；
- 缓存里存的字节与真实响应**逐字节一致**（同一份 JSONBytes 既写缓存也返回，见 api 层）；
- 空搜索结果**不写缓存**：空往往来自第三方临时抽风，缓存住会让搜索一小时全空。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gateway.adapters.quote.kline import fetch_bars, period_supported
from gateway.adapters.quote.search import search as upstream_search
from gateway.domain.amount import money
from gateway.domain.stockcode import normalize_code
from gateway.infra import cache
from gateway.infra.deps import runtime
from gateway.infra.resp import (
    CODE_BAD_GATEWAY,
    CODE_BAD_REQUEST,
    CODE_INTERNAL,
    CODE_NOT_FOUND,
    CODE_OK,
    ApiError,
    json_bytes,
)
from gateway.repositories import stock as repo

logger = logging.getLogger("stocka.gateway.stock")

SEARCH_CACHE_TTL = 3600
QUOTE_CACHE_TTL = 30
KLINE_CACHE_TTL = 30
PROFILE_CACHE_TTL = 3600
MAX_LIMIT = 10
MAX_SEARCH_RUNES = 32
KLINE_DEFAULT_LIMIT = 60
KLINE_MAX_LIMIT = 120
KLINE_PERIODS = "周期只支持 m1 / m5 / m15 / day / week"

_CODE_MESSAGE = "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字"


def normalize_limit(limit: int) -> int:
    return 5 if limit <= 0 else min(limit, MAX_LIMIT)


def check_keyword(keyword: str) -> str:
    keyword = (keyword or "").strip()
    if len(keyword) > MAX_SEARCH_RUNES:
        raise ApiError(CODE_BAD_REQUEST, "搜索词过长")
    return keyword


# ---------------------------------------------------------------- 搜索


async def search_payload(keyword: str, limit: int) -> tuple[list[dict[str, str]], str]:
    """返回 (对外 payload, 数据来源)；来源只用于 X-Search-Source 响应头。"""
    results = await upstream_search(keyword, limit)
    source = "tencent"
    if not results:
        results = await repo.search_local(keyword, limit)
        source = "db"
    if not results:
        source = "empty"
    return [{"code": r.code, "market": r.market, "name": r.name} for r in results], source


async def service_search(keyword: str, limit: int) -> list[dict[str, str]]:
    """供 agent 工具层直调（等价于原 HTTP 版 /api/v1/stocks/search 的 data）。"""
    keyword = check_keyword(keyword)
    limit = normalize_limit(limit)
    payload, _ = await search_payload(keyword, limit)
    return payload


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


def normalize_stock_code(code: str) -> str:
    normalized, ok = normalize_code(code)
    if not ok:
        raise ApiError(CODE_BAD_REQUEST, _CODE_MESSAGE)
    return normalized


async def quote_payload(normalized: str) -> dict[str, Any]:
    detail = await runtime().service("market_source").fetch_detail(normalized)
    if detail is None:
        raise ApiError(CODE_BAD_GATEWAY, "行情获取失败，请稍后重试")
    return _detail_payload(detail)


async def service_quote(code: str) -> dict[str, Any]:
    """供 agent 工具层直调（等价于原 HTTP 版 /api/v1/stock/quote 的 data）。"""
    normalized = normalize_stock_code(code)
    cache_key = f"stock:quote:{normalized}"
    cached = await cache.get_bytes(cache_key)
    if cached:
        envelope = json.loads(cached.decode("utf-8"))
        return envelope.get("data") or {}

    payload = await quote_payload(normalized)
    # set_bytes 内部已吞掉异常：缓存写失败不能影响行情返回
    await cache.set_bytes(cache_key, json_bytes(CODE_OK, "ok", payload), QUOTE_CACHE_TTL)
    return payload


# ---------------------------------------------------------------- K 线


def normalize_period(period: str) -> str:
    period = (period or "").strip() or "day"
    if not period_supported(period):
        raise ApiError(CODE_BAD_REQUEST, KLINE_PERIODS)
    return period


async def kline_payload(code: str, period: str, limit: int) -> tuple[str, str, int, dict[str, Any]]:
    """返回 (normalized, period, limit, payload)，调用方据此拼缓存 key。"""
    normalized = normalize_stock_code(code)
    period = normalize_period(period)
    limit = KLINE_DEFAULT_LIMIT if limit <= 0 else min(limit, KLINE_MAX_LIMIT)

    try:
        bars = await fetch_bars(normalized, period, limit)
    except Exception as exc:
        logger.info("fetch kline failed: code=%s period=%s err=%s", normalized, period, exc)
        raise ApiError(CODE_BAD_GATEWAY, "K线获取失败，请稍后重试") from exc
    if not bars:
        raise ApiError(CODE_BAD_GATEWAY, "K线获取失败，请稍后重试")

    payload = {
        "code": normalized,
        "period": period,
        "bars": [
            {"d": b.d, "o": str(b.o), "h": str(b.h), "l": str(b.l), "c": str(b.c), "v": b.v}
            for b in bars
        ],
    }
    return normalized, period, limit, payload


# ---------------------------------------------------------------- 主数据


async def profile_payload(code: str) -> tuple[str, dict[str, Any]]:
    """返回 (digits, payload)：digits 用于拼缓存 key。"""
    normalized = normalize_stock_code(code)
    digits = normalized[2:]
    try:
        profile = await repo.fetch_profile(digits)
    except Exception as exc:
        logger.info("fetch stock profile failed: code=%s err=%s", digits, exc)
        raise ApiError(CODE_INTERNAL, "主数据查询失败，请稍后重试") from exc

    if profile is None:
        raise ApiError(CODE_NOT_FOUND, "没有该股票的主数据，请先执行 sync-master")
    # omitempty：空值不下发，前端有兜底
    return digits, {
        k: v for k, v in profile.items() if not (k.endswith("_date") and v == "")
    }
