"""K 线拉取与解析。

行格式是 **[时间, 开, 收, 高, 低, 量]**——注意不是 OHLC 顺序。
前复权数据放在 qfqday / qfqweek，只有不复权时才是 day / week。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from server.deps import runtime
from server.domain.stockcode import normalize_code
from server.quote.source import KLINE_DAILY_URL, KLINE_MINUTE_URL

logger = logging.getLogger("stocka.server.quote.kline")

# period 来自客户端，绝不能直接拼进上游 URL（拼串等于把上游地址交给调用方）
KLINE_PERIODS = frozenset({"m1", "m5", "m15", "day", "week"})


@dataclass(slots=True)
class KBar:
    d: str
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    v: int


def period_supported(period: str) -> bool:
    return period in KLINE_PERIODS


def _raw_str(value: object) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _raw_decimal(value: object) -> tuple[Decimal, bool]:
    try:
        return Decimal(_raw_str(value).strip()), True
    except (InvalidOperation, ValueError):
        return Decimal(0), False


def _parse_rows(rows: object) -> list[KBar]:
    if not isinstance(rows, list):
        return []
    out: list[KBar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        day = _raw_str(row[0])
        open_, ok_open = _raw_decimal(row[1])
        close, ok_close = _raw_decimal(row[2])
        high, ok_high = _raw_decimal(row[3])
        low, ok_low = _raw_decimal(row[4])
        volume, _ = _raw_decimal(row[5])
        # 停牌 / 空窗会出现空串价格，整行丢掉，别让 0 值 K 线把图上比例拉歪
        if not day or not (ok_open and ok_close and ok_high and ok_low):
            continue
        out.append(
            KBar(d=day, o=open_, h=high, l=low, c=close, v=int(volume))
        )
    return out


def _parse_bars(body: bytes, symbol: str, period: str) -> list[KBar]:
    try:
        top = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    if not isinstance(top, dict):
        return []
    payload = (top.get("data") or {}).get(symbol)
    if not isinstance(payload, dict):
        return []

    keys = [period]
    if period == "day":
        keys = ["qfqday", "day"]
    elif period == "week":
        keys = ["qfqweek", "week"]

    for key in keys:
        if key in payload:
            bars = _parse_rows(payload[key])
            if bars:
                return bars
    return []


async def fetch_bars(code: str, period: str, limit: int) -> list[KBar]:
    """拉最近 limit 根 K 线，按上游返回顺序（时间升序）。"""
    if period not in KLINE_PERIODS:
        raise ValueError(f"unsupported period {period!r}")
    normalized, ok = normalize_code(code)
    if not ok:
        raise ValueError(f"invalid stock code {code!r}")
    symbol = normalized.lower()

    if period in ("day", "week"):
        url = f"{KLINE_DAILY_URL}{symbol},{period},,,{limit},qfq"
    else:
        url = f"{KLINE_MINUTE_URL}{symbol},{period},,{limit}"

    body = await runtime().service("market_source").get_bytes(url)
    # 这两个接口都返回 UTF-8 JSON（只有行情文本接口是 GBK），不需要转码
    return _parse_bars(body, symbol, period)
