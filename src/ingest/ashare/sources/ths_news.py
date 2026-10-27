"""同花顺 7×24 快讯采集。

akshare 只返回最新一批（约数十条），和财联社一样必须高频轮询；
不同之处是每条带原文链接，可直接作为溯源地址。
"""
import logging
from collections.abc import Iterable
from datetime import datetime

from ingest.ashare import clean, config
from ingest.ashare.sources.base import BaseSource
from ingest.ashare.types import RawDoc
from ingest.ashare.util import cell, md5_hex, parse_datetime, sha1_hex

logger = logging.getLogger(__name__)


class ThsGlobalNews(BaseSource):
    name = "ths"
    watermark_mode = "latest"

    def __init__(self):
        self.default_backfill_days = config.BACKFILL_DAYS["ths"]

    def fetch(
        self,
        since: datetime,
        until: datetime,
        limit: int | None = None,
        codes: Iterable[str] | None = None,
    ) -> Iterable[RawDoc]:
        import akshare as ak

        if not hasattr(ak, "stock_info_global_ths"):
            raise RuntimeError("当前 akshare 版本没有 stock_info_global_ths，请升级或禁用 ths 源")

        df = ak.stock_info_global_ths()
        if df is None or df.empty:
            return

        yielded = 0
        for _, row in df.iterrows():
            title = cell(row, "标题", "title")
            content = cell(row, "内容", "content", "摘要")
            url = cell(row, "链接", "url")
            pub = parse_datetime(cell(row, "发布时间", "时间"))
            if not title and not content:
                continue
            if pub and pub < since:
                continue
            title = title or clean.guess_title(content)
            yield RawDoc(
                source=self.name,
                source_id=md5_hex(url) if url else sha1_hex(title, str(pub), content)[:24],
                source_type="news_flash",
                title=title,
                pub_time=pub,
                url=url,
                content=content or title,
                trust=0.8,
                event_type="快讯",
            )
            yielded += 1
            if limit and yielded >= limit:
                return

        logger.info("同花顺快讯抓取 %d 条", yielded)
