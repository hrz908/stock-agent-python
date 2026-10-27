"""腾讯财经行情源。

踩过的坑都在这里固化了：
- 批量行情接口 qt.gtimg.cn 返回 **GBK**，不转码中文股票名全乱码；
- 搜索接口 smartbox 返回**纯 ASCII + \\uXXXX 转义**，绝不能套 GBK 解码器；
- 分钟 K 线必须用 ifzq.gtimg.cn，换成 web.ifzq.gtimg.cn 会 301；
- 所有响应都限长读取，上游故障返回 GB 级内容会直接打爆内存。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

from gateway.domain.amount import yi_to_yuan
from gateway.domain.stockcode import normalize_code
from gateway.infra.config import HTTPConfig

logger = logging.getLogger("stocka.gateway.quote")

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
SMARTBOX_URL = "https://smartbox.gtimg.cn/s3/"
KLINE_MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param="
KLINE_DAILY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
SINA_RANK_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)

MAX_BODY_BYTES = 1 << 20
USER_AGENT = "Mozilla/5.0 (stock-agent-miniapp)"

# 腾讯批量行情串里按 ~ 分隔的字段下标
TEC_NAME = 1
TEC_CODE = 2
TEC_PRICE = 3
TEC_PREV_CLOSE = 4
TEC_OPEN = 5
TEC_VOLUME = 6
TEC_TIME = 30
TEC_CHANGE = 31
TEC_PERCENT = 32
TEC_HIGH = 33
TEC_LOW = 34
TEC_AMOUNT = 37
TEC_TURNOVER = 38
TEC_PE = 39
TEC_FREE_MV = 44
TEC_TOTAL_MV = 45
TEC_PB = 46
TEC_VOLUME_RATIO = 49

_TENCENT_VAR_RE = re.compile(r'v_[a-z0-9]+="([^"]*)"')

# A 股行情时间基准时区：固定 +8，不依赖服务器本地时区
CST = timezone(timedelta(hours=8))


@dataclass(slots=True)
class StockSnapshot:
    code: str
    name: str
    price: Decimal
    change_percent: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "name": self.name,
            "price": str(self.price),
            "change_percent": str(self.change_percent),
        }

    @classmethod
    def from_dict(cls, data: dict) -> StockSnapshot:
        return cls(
            code=str(data.get("code") or ""),
            name=str(data.get("name") or ""),
            price=_decimal(data.get("price")),
            change_percent=_decimal(data.get("change_percent")),
        )


@dataclass(slots=True)
class QuoteDetail:
    """单只股票的完整行情快照（行情详情页与 agent 同业对比工具共用）。"""

    code: str
    name: str
    price: Decimal
    change: Decimal
    change_percent: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    volume: int
    amount: Decimal
    turnover: Decimal
    volume_ratio: Decimal
    pe: Decimal
    pb: Decimal
    total_mv: Decimal
    free_mv: Decimal
    status: str
    time: str


def _decimal(raw: object) -> Decimal:
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def format_quote_time(raw: str) -> str:
    """20261020150003 → 2026-10-20 15:00:03；长度不符原样返回，不做臆测。"""
    if len(raw) != 14:
        return raw
    return (
        f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} "
        f"{raw[8:10]}:{raw[10:12]}:{raw[12:14]}"
    )


def market_status(now: datetime | None = None) -> str:
    """按交易时段粗判（周一至五 9:30-11:30 / 13:00-15:00），不含节假日日历。

    详情页只需区分「是否交易时段」，不值得为此维护一张节假日表。
    """
    moment = (now or datetime.now(UTC)).astimezone(CST)
    if moment.weekday() >= 5:
        return "closed"
    minutes = moment.hour * 60 + moment.minute
    morning = 9 * 60 + 30 <= minutes < 11 * 60 + 30
    afternoon = 13 * 60 <= minutes < 15 * 60
    return "trading" if morning or afternoon else "closed"


def parse_tencent_quotes(body: str) -> list[StockSnapshot]:
    out: list[StockSnapshot] = []
    for match in _TENCENT_VAR_RE.finditer(body):
        whole = match.group(0)
        symbol = whole[: whole.index('="')].upper()
        if "_" in symbol:
            symbol = symbol.split("_", 1)[1]

        fields = match.group(1).split("~")
        if len(fields) <= TEC_PERCENT:
            continue

        raw_price = fields[TEC_PRICE].strip()
        try:
            price = Decimal(raw_price)
        except (InvalidOperation, ValueError):
            # 停牌 / 退市 / 代码不存在时价格字段为空，按无行情处理
            logger.info("parse quote price failed: code=%s raw=%r", symbol, raw_price)
            continue

        try:
            percent = Decimal(fields[TEC_PERCENT].strip())
        except (InvalidOperation, ValueError):
            percent = Decimal(0)

        name = fields[TEC_NAME].strip() or symbol
        out.append(
            StockSnapshot(code=symbol, name=name, price=price, change_percent=percent)
        )
    return out


def parse_tencent_details(body: str) -> list[QuoteDetail]:
    out: list[QuoteDetail] = []
    status = market_status()

    for match in _TENCENT_VAR_RE.finditer(body):
        whole = match.group(0)
        symbol = whole[: whole.index('="')].upper()
        if "_" in symbol:
            symbol = symbol.split("_", 1)[1]

        fields = match.group(1).split("~")
        if len(fields) <= TEC_TOTAL_MV:
            continue

        raw_price = fields[TEC_PRICE].strip()
        if not raw_price:
            logger.info("parse quote detail failed: code=%s empty price", symbol)
            continue

        name = fields[TEC_NAME].strip() or symbol
        volume_ratio = (
            _decimal(fields[TEC_VOLUME_RATIO])
            if len(fields) > TEC_VOLUME_RATIO
            else Decimal(0)
        )

        out.append(
            QuoteDetail(
                code=symbol,
                name=name,
                price=_decimal(raw_price),
                change=_decimal(fields[TEC_CHANGE]),
                change_percent=_decimal(fields[TEC_PERCENT]),
                open=_decimal(fields[TEC_OPEN]),
                high=_decimal(fields[TEC_HIGH]),
                low=_decimal(fields[TEC_LOW]),
                volume=int(_decimal(fields[TEC_VOLUME])),
                amount=_decimal(fields[TEC_AMOUNT]),
                turnover=_decimal(fields[TEC_TURNOVER]),
                volume_ratio=volume_ratio,
                pe=_decimal(fields[TEC_PE]),
                pb=_decimal(fields[TEC_PB]),
                total_mv=yi_to_yuan(_decimal(fields[TEC_TOTAL_MV])),
                free_mv=yi_to_yuan(_decimal(fields[TEC_FREE_MV])),
                status=status,
                time=format_quote_time(fields[TEC_TIME].strip()),
            )
        )
    return out


class MarketSource:
    """行情源客户端：复用连接，超时按用途分档。"""

    def __init__(self, cfg: HTTPConfig, quote_api: str = "") -> None:
        self._cfg = cfg
        self._custom_api = (quote_api or "").strip()
        self._quote_client = httpx.AsyncClient(
            timeout=cfg.quote_timeout, headers={"User-Agent": USER_AGENT}
        )
        # 搜索超时比行情宽：宁可多等一会儿，也不要假超时返回空列表
        self._search_client = httpx.AsyncClient(
            timeout=cfg.search_timeout, headers={"User-Agent": USER_AGENT}
        )

    @property
    def uses_custom_api(self) -> bool:
        return bool(self._custom_api)

    async def close(self) -> None:
        await self._quote_client.aclose()
        await self._search_client.aclose()

    @property
    def search_client(self) -> httpx.AsyncClient:
        return self._search_client

    async def fetch_raw(self, url: str) -> str:
        """GET 腾讯行情类接口并做 GBK → UTF-8 转码。"""
        response = await self._quote_client.get(url)
        if response.status_code != 200:
            raise RuntimeError(f"quote api http {response.status_code}")
        body = response.content[:MAX_BODY_BYTES]
        return body.decode("gbk", errors="replace")

    async def get_bytes(self, url: str) -> bytes:
        """取原始响应字节（K 线这类 UTF-8 JSON 接口用）。"""
        response = await self._quote_client.get(url)
        if response.status_code != 200:
            raise RuntimeError(f"upstream http {response.status_code}")
        return response.content[:MAX_BODY_BYTES]

    async def fetch_snapshots(self, codes: list[str]) -> list[StockSnapshot]:
        """批量拉快照，只返回解析成功的部分（失败集合由调用方求差集）。"""
        if not codes:
            return []
        if self._custom_api:
            return await self._fetch_from_custom(codes)

        symbols: list[str] = []
        for code in codes:
            normalized, ok = normalize_code(code)
            if not ok:
                logger.info("skip invalid code in quote url: %r", code)
                continue
            symbols.append(normalized.lower())
        if not symbols:
            return []

        body = await self.fetch_raw(TENCENT_QUOTE_URL + ",".join(symbols))
        return parse_tencent_quotes(body)

    async def fetch_snapshot(self, code: str) -> StockSnapshot | None:
        snaps = await self.fetch_snapshots([code])
        return snaps[0] if snaps else None

    async def fetch_detail(self, code: str) -> QuoteDetail | None:
        if self._custom_api:
            snap = await self.fetch_snapshot(code)
            if snap is None:
                return None
            return QuoteDetail(
                code=snap.code,
                name=snap.name,
                price=snap.price,
                change=Decimal(0),
                change_percent=snap.change_percent,
                open=Decimal(0),
                high=Decimal(0),
                low=Decimal(0),
                volume=0,
                amount=Decimal(0),
                turnover=Decimal(0),
                volume_ratio=Decimal(0),
                pe=Decimal(0),
                pb=Decimal(0),
                total_mv=Decimal(0),
                free_mv=Decimal(0),
                status=market_status(),
                time="",
            )

        normalized, ok = normalize_code(code)
        if not ok:
            return None
        body = await self.fetch_raw(TENCENT_QUOTE_URL + normalized.lower())
        details = parse_tencent_details(body)
        return details[0] if details else None

    async def _fetch_from_custom(self, codes: list[str]) -> list[StockSnapshot]:
        """自建行情服务：批内并发。逐只串行时一批 20 只 × 3s 最坏要 60s。"""
        import asyncio

        semaphore = asyncio.Semaphore(max(1, self._cfg.quote_fetch_limit))

        async def one(code: str) -> StockSnapshot | None:
            async with semaphore:
                try:
                    response = await self._quote_client.get(
                        self._custom_api, params={"code": code}
                    )
                    if response.status_code != 200:
                        return None
                    payload = response.json()
                except Exception as exc:  # noqa: BLE001 - 单只失败不影响同批其它
                    logger.info("fetch quote %s failed: %s", code, exc)
                    return None
                name = str(payload.get("name") or "").strip() or code
                return StockSnapshot(
                    code=code,
                    name=name,
                    price=_decimal(payload.get("price")),
                    change_percent=_decimal(payload.get("change_percent")),
                )

        results = await asyncio.gather(*(one(code) for code in codes))
        return [item for item in results if item is not None]
