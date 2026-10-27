"""东方财富研报采集（reportapi 公开 JSON）。

qType：0 个股研报、1 行业研报、2 策略报告、3 宏观研究。个股研报只有标题和评级，
正文要下 PDF；行业/策略/宏观同理。默认三者的 PDF 都抓，标题级元数据不足以支撑问答。
"""
import logging
import re
from collections.abc import Iterable
from datetime import datetime

from ingest.ashare import config
from ingest.ashare.net import HttpClient
from ingest.ashare.sources.base import BaseSource
from ingest.ashare.types import RawDoc
from ingest.ashare.util import parse_datetime, sha1_hex

logger = logging.getLogger(__name__)

API_URL = "https://reportapi.eastmoney.com/report/list"
PDF_URL = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
_SCOPE = {0: "个股", 1: "行业", 2: "策略", 3: "宏观"}

_REPORT_RULES = (
    ("晨会", "晨会纪要"),
    ("策略", "策略报告"),
    ("宏观", "宏观研究"),
    ("深度", "深度报告"),
    ("首次覆盖", "首次覆盖"),
    ("点评", "点评报告"),
    ("跟踪", "跟踪报告"),
)


def _report_type(title: str, qtype: int) -> str:
    for keyword, label in _REPORT_RULES:
        if keyword in (title or ""):
            return label
    return f"{_SCOPE.get(qtype, '')}研报".strip() or "研究报告"


class EastmoneyReport(BaseSource):
    name = "em-report"
    watermark_mode = "window"

    def __init__(self):
        self.http = HttpClient(qps=config.EASTMONEY_QPS)
        self.qtypes = [int(x) for x in config.REPORT_QTYPES.split(",") if x.strip().isdigit()]
        self.default_backfill_days = config.BACKFILL_DAYS["em-report"]

    def _page(self, qtype: int, page: int, begin: str, end: str) -> dict:
        params = {
            "industryCode": "*",
            "pageSize": config.REPORT_PAGE_SIZE,
            "industry": "*",
            "rating": "",
            "ratingChange": "",
            "beginTime": begin,
            "endTime": end,
            "pageNo": page,
            "fields": "",
            "qType": qtype,
            "orgCode": "",
            "code": "*",
            "rcode": "",
            "p": page,
            "pageNum": page,
        }
        payload = self.http.get_json(API_URL, params=params, headers={
            "Referer": "https://data.eastmoney.com/report/",
            "Accept": "application/json, text/plain, */*",
        })
        if not isinstance(payload, dict):
            raise RuntimeError(f"东财研报返回结构异常：{type(payload)}")
        return payload

    def fetch(
        self,
        since: datetime,
        until: datetime,
        limit: int | None = None,
        codes: Iterable[str] | None = None,
    ) -> Iterable[RawDoc]:
        code_filter = {str(c).zfill(6) for c in (codes or []) if str(c).strip()} or None
        begin, end = f"{since:%Y-%m-%d}", f"{until:%Y-%m-%d}"
        yielded = 0
        seen: set[str] = set()

        for qtype in self.qtypes:
            page = 1
            while page <= config.REPORT_MAX_PAGES:
                payload = self._page(qtype, page, begin, end)
                rows = payload.get("data") or []
                if not rows:
                    break
                for row in rows:
                    doc = self._to_doc(row, qtype)
                    if doc is None or doc.source_id in seen:
                        continue
                    # publishDate 只精确到日，按时间戳过滤会漏掉当天增量
                    if doc.pub_time and doc.pub_time.date() < since.date():
                        continue
                    if code_filter and not (set(doc.stock_codes) & code_filter):
                        continue
                    seen.add(doc.source_id)
                    yield doc
                    yielded += 1
                    if limit and yielded >= limit:
                        return
                if len(rows) < config.REPORT_PAGE_SIZE:
                    break
                page += 1
            logger.info("东财研报 qType=%s 窗口 %s~%s 完成", qtype, begin, end)

        logger.info("东财研报共抓取 %d 条", yielded)

    def _to_doc(self, row: dict, qtype: int) -> RawDoc | None:
        title = str(row.get("title") or row.get("reportName") or "").strip()
        if not title:
            return None
        info_code = str(row.get("infoCode") or "").strip()
        code = re.sub(r"\D", "", str(row.get("stockCode") or ""))[:6]
        pub = parse_datetime(row.get("publishDate") or row.get("pubDate"))
        source_id = info_code or sha1_hex(title, str(pub), str(row.get("orgSName") or ""))
        pdf_url = PDF_URL.format(info_code=info_code) if info_code else ""

        return RawDoc(
            source=self.name,
            source_id=source_id,
            source_type="research_report",
            title=title,
            pub_time=pub,
            url=f"https://data.eastmoney.com/report/zw_industry.jshtml?infocode={info_code}" if info_code else "",
            pdf_url=pdf_url,
            stock_codes=[code] if code else [],
            stock_names=[str(row.get("stockName") or "").strip()] if row.get("stockName") else [],
            industry=str(row.get("industryName") or row.get("indvInduName") or "").strip(),
            org=str(row.get("orgSName") or row.get("orgName") or "").strip(),
            author=str(row.get("researcher") or row.get("author") or "").strip(),
            rating=str(row.get("emRatingName") or row.get("sRatingName") or "").strip(),
            event_type=_report_type(title, qtype),
            trust=0.9,
            extra={
                "report_scope": _SCOPE.get(qtype, ""),
                "info_code": info_code,
                "target_price": row.get("indvAimPriceT"),
            },
        )
