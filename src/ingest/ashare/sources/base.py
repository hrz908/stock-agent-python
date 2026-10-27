"""采集器基类。"""
from collections.abc import Iterable
from datetime import datetime

from ingest.ashare.types import RawDoc


class BaseSource:
    name: str = ""
    # window：按日期区间查询，水位推进到窗口右端；latest：只给最新若干条，水位取最大发布时间
    watermark_mode: str = "window"
    default_backfill_days: int = 1

    def fetch(
        self,
        since: datetime,
        until: datetime,
        limit: int | None = None,
        codes: Iterable[str] | None = None,
    ) -> Iterable[RawDoc]:
        raise NotImplementedError
