"""财联社电报采集。

akshare 只给「最近 20 条」，所以这个源必须高频轮询（默认 5 分钟），
靠 source_id（时间+正文哈希）与指纹去重，不能指望一次抓到一天的量。
"""
import logging
from collections.abc import Iterable
from datetime import datetime

from ingest.ashare import clean, config
from ingest.ashare.sources.base import BaseSource
from ingest.ashare.types import RawDoc
from ingest.ashare.util import cell, parse_datetime, sha1_hex

logger = logging.getLogger(__name__)


class ClsTelegraph(BaseSource):
    name = "cls"
    watermark_mode = "latest"

    def __init__(self):
        self.default_backfill_days = config.BACKFILL_DAYS["cls"]

    def fetch(
        self,
        since: datetime,
        until: datetime,
        limit: int | None = None,
        codes: Iterable[str] | None = None,
    ) -> Iterable[RawDoc]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("未安装 akshare，无法采集财联社电报") from exc

        df = ak.stock_info_global_cls(symbol=config.CLS_SYMBOL)
        if df is None or df.empty:
            logger.info("财联社电报为空")
            return

        yielded = 0
        for _, row in df.iterrows():
            content = cell(row, "内容", "content")
            title = cell(row, "标题", "title")
            date_text = cell(row, "发布日期", "date")
            time_text = cell(row, "发布时间", "time")
            pub = parse_datetime(f"{date_text} {time_text}".strip()) if time_text else parse_datetime(date_text)

            if not content and not title:
                continue
            if pub and pub < since:
                continue
            title = title or clean.guess_title(content)
            content = content or title

            yield RawDoc(
                source=self.name,
                source_id=sha1_hex(str(pub), title, content)[:24],
                source_type="news_flash",
                title=title,
                pub_time=pub,
                url="https://www.cls.cn/telegraph",
                content=content,
                trust=0.85,
                event_type="电报",
                extra={"channel": config.CLS_SYMBOL},
            )
            yielded += 1
            if limit and yielded >= limit:
                return

        logger.info("财联社电报抓取 %d 条", yielded)
