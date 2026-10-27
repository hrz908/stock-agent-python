"""批量拉行情：去重 → 分批 → 批间并发。

一条铁律：**单批失败绝不返回错误、也不取消其它批次**。
一旦让异常冒泡（Go 里是 errgroup 取消 gctx），一只停牌股就能让整次刷新
退回全量旧缓存。失败集合靠「去重后请求列表 − 成功集合」求差集得到。
"""

from __future__ import annotations

import asyncio
import logging

from server.deps import runtime
from server.quote.source import QuoteDetail, StockSnapshot, market_status
from server.util.unique import chunk, unique

logger = logging.getLogger("stocka.server.quote.batch")


async def fetch_quotes(codes: list[str]) -> tuple[dict[str, StockSnapshot], list[str]]:
    """返回 (成功快照, 失败代码列表)。"""
    out: dict[str, StockSnapshot] = {}
    if not codes:
        return out, []

    cfg = runtime().cfg.http
    source = runtime().service("market_source")
    uniq = unique(codes)
    semaphore = asyncio.Semaphore(max(1, cfg.quote_fetch_limit))

    async def one_batch(batch: list[str]) -> None:
        async with semaphore:
            try:
                snaps = await source.fetch_snapshots(batch)
            except Exception as exc:  # noqa: BLE001 - 单批失败只丢该批
                logger.info("fetch quote batch failed: %s", exc)
                return
        for snap in snaps:
            out[snap.code] = snap

    await asyncio.gather(*(one_batch(group) for group in chunk(uniq, cfg.quote_batch_size)))

    # 上游返回的 code 一定是归一化后的形式，与请求列表对齐后再求差集
    failed = [code for code in uniq if code not in out]
    return out, failed


async def fetch_details(codes: list[str]) -> dict[str, QuoteDetail]:
    """批量拉完整行情（含成交额/换手率/量比），供热度池这类全池打分场景用。

    自建源只约定 4 个字段：量额等全为零，调用方需按「实际有值的信号」
    重新归一化权重，否则整池分数会被压平。
    """
    out: dict[str, QuoteDetail] = {}
    if not codes:
        return out

    cfg = runtime().cfg.http
    source = runtime().service("market_source")
    uniq = unique(codes)

    if source.uses_custom_api:
        from decimal import Decimal

        zero = Decimal(0)
        snaps, _ = await fetch_quotes(uniq)
        status = market_status()
        for code, snap in snaps.items():
            out[code] = QuoteDetail(
                code=code,
                name=snap.name,
                price=snap.price,
                change=zero,
                change_percent=snap.change_percent,
                open=zero,
                high=zero,
                low=zero,
                volume=0,
                amount=zero,
                turnover=zero,
                volume_ratio=zero,
                pe=zero,
                pb=zero,
                total_mv=zero,
                free_mv=zero,
                status=status,
                time="",
            )
        return out

    symbols: list[str] = []
    for code in uniq:
        from server.domain.stockcode import normalize_code

        normalized, ok = normalize_code(code)
        if not ok:
            logger.info("skip invalid code in detail url: %r", code)
            continue
        symbols.append(normalized.lower())
    if not symbols:
        return out

    semaphore = asyncio.Semaphore(max(1, cfg.quote_fetch_limit))

    async def one_batch(batch: list[str]) -> None:
        async with semaphore:
            try:
                body = await source.fetch_raw(
                    "https://qt.gtimg.cn/q=" + ",".join(batch)
                )
                from server.quote.source import parse_tencent_details

                details = parse_tencent_details(body)
            except Exception as exc:  # noqa: BLE001 - 一批超时不该让整池只剩残缺数据
                logger.info("fetch quote details failed: %s", exc)
                return
        for detail in details:
            out[detail.code] = detail

    await asyncio.gather(*(one_batch(group) for group in chunk(symbols, cfg.quote_batch_size)))
    return out
