"""采集与入库共用的数据结构。"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# source_type 是检索过滤与质量门禁的主键，新增类型必须同步 clean/quality 的阈值表
SOURCE_TYPE_LABELS = {
    "annual_report": "年度报告",
    "semi_annual_report": "半年度报告",
    "quarterly_report": "季度报告",
    "announcement": "公司公告",
    "research_report": "研究报告",
    "industry_policy": "行业政策",
    "news_flash": "市场快讯",
    "news": "公司新闻",
}

PERIODIC_TYPES = ("annual_report", "semi_annual_report", "quarterly_report")


@dataclass(slots=True)
class RawDoc:
    """采集到的原始条目。正文可为空（需按 pdf_url 二次抓取）。"""

    source: str
    source_id: str
    source_type: str
    title: str
    pub_time: datetime | None = None
    url: str = ""
    content: str = ""
    pdf_url: str = ""
    stock_codes: list[str] = field(default_factory=list)
    stock_names: list[str] = field(default_factory=list)
    industry: str = ""
    org: str = ""
    author: str = ""
    rating: str = ""
    event_type: str = ""
    trust: float = 0.9
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def source_path(self) -> str:
        """documents.source_path 的规范形式：源+源侧 ID，天然幂等。"""
        return f"ashare://{self.source}/{self.source_id}"


@dataclass(slots=True)
class Chunk:
    seq: int
    content: str
    section: str
    token_count: int
    embedding: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunStats:
    source: str = ""
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    chunks: int = 0
    last_pub: datetime | None = None

    def merge(self, other: "RunStats") -> None:
        self.fetched += other.fetched
        self.written += other.written
        self.skipped += other.skipped
        self.failed += other.failed
        self.chunks += other.chunks
        if other.last_pub and (self.last_pub is None or other.last_pub > self.last_pub):
            self.last_pub = other.last_pub

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "fetched": self.fetched,
            "written": self.written,
            "skipped": self.skipped,
            "failed": self.failed,
            "chunks": self.chunks,
        }
