"""东方财富资讯：市场级全球快讯 + 可选个股新闻。

个股新闻按股票逐个请求，一次只能拿最近几十条；全市场扫一遍要一小时以上，
所以默认不跑，必须显式给 --codes 或设置 ASHARE_NEWS_CODES。
"""
import logging
import time
from collections.abc import Iterable
from datetime import datetime

from ingest.ashare import clean, config
from ingest.ashare.sources.base import BaseSource
from ingest.ashare.types import RawDoc
from ingest.ashare.util import cell, md5_hex, parse_datetime, sha1_hex, split_csv

logger = logging.getLogger(__name__)


class EastmoneyGlobalNews(BaseSource):
    name = "em-global"
    watermark_mode = "latest"

    def __init__(self):
        self.default_backfill_days = config.BACKFILL_DAYS["em-global"]

    def fetch(
        self,
        since: datetime,
        until: datetime,
        limit: int | None = None,
        codes: Iterable[str] | None = None,
    ) -> Iterable[RawDoc]:
        import akshare as ak

        df = ak.stock_info_global_em()
        if df is None or df.empty:
            return

        yielded = 0
        for _, row in df.iterrows():
            title = cell(row, "标题", "title")
            summary = cell(row, "摘要", "内容", "summary")
            url = cell(row, "链接", "url")
            pub = parse_datetime(cell(row, "发布时间", "时间", "date"))
            if not title and not summary:
                continue
            if pub and pub < since:
                continue
            title = title or clean.guess_title(summary)
            yield RawDoc(
                source=self.name,
                source_id=md5_hex(url) if url else sha1_hex(title, str(pub))[:24],
                source_type="news_flash",
                title=title,
                pub_time=pub,
                url=url,
                content=summary or title,
                trust=0.8,
                event_type="快讯",
            )
            yielded += 1
            if limit and yielded >= limit:
                return

        logger.info("东财全球快讯抓取 %d 条", yielded)


class EastmoneyStockNews(BaseSource):
    name = "em-news"
    watermark_mode = "latest"

    def __init__(self):
        self.default_backfill_days = config.BACKFILL_DAYS["em-news"]

    def fetch(
        self,
        since: datetime,
        until: datetime,
        limit: int | None = None,
        codes: Iterable[str] | None = None,
    ) -> Iterable[RawDoc]:
        import akshare as ak

        target = [c.zfill(6) for c in (codes or split_csv(config.NEWS_CODES)) if str(c).strip()]
        if not target:
            logger.info("未指定个股代码（--codes / ASHARE_NEWS_CODES），跳过个股新闻")
            return
        if config.NEWS_MAX_CODES > 0:
            target = target[: config.NEWS_MAX_CODES]

        names: dict[str, str] = {}
        try:
            from ingest.ashare.universe import load_universe
            names = load_universe()
        except Exception as exc:  # 代码表失败不影响正文采集
            logger.warning("加载代码表失败，公司简称留空：%s", exc)

        interval = 1.0 / config.EASTMONEY_QPS if config.EASTMONEY_QPS > 0 else 0.0
        yielded = 0
        seen: set[str] = set()

        for code in target:
            try:
                df = ak.stock_news_em(symbol=code)
            except Exception as exc:
                logger.warning("个股新闻抓取失败 %s：%s", code, exc)
                time.sleep(interval)
                continue

            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    title = cell(row, "新闻标题", "title")
                    content = cell(row, "新闻内容", "内容", "摘要")
                    url = cell(row, "新闻链接", "链接", "url")
                    pub = parse_datetime(cell(row, "发布时间", "时间"))
                    if not title:
                        continue
                    if pub and pub < since:
                        continue
                    sid = md5_hex(url) if url else sha1_hex(code, title, str(pub))[:24]
                    if sid in seen:
                        continue
                    seen.add(sid)
                    yield RawDoc(
                        source=self.name,
                        source_id=sid,
                        source_type="news",
                        title=title,
                        pub_time=pub,
                        url=url,
                        content=content or title,
                        stock_codes=[code],
                        stock_names=[names.get(code, "")],
                        org=cell(row, "文章来源", "来源"),
                        event_type="公司新闻",
                        trust=0.8,
                        extra={"keyword": cell(row, "关键词")},
                    )
                    yielded += 1
                    if limit and yielded >= limit:
                        return
            time.sleep(interval)

        logger.info("个股新闻抓取 %d 条（%d 只）", yielded, len(target))
