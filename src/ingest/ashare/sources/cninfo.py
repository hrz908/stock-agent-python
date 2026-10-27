"""巨潮资讯公告采集：公告 + 年报/半年报/季报，直接拿 PDF 原文链接。

选巨潮而非东财公告列表的原因：东财列表只给详情页 HTML，正文还得再猜一次内嵌；
巨潮的 hisAnnouncement/query 直接返回 adjunctUrl，拼接即得 static.cninfo.com.cn 上的 PDF，
少一层解析就少一类失败。
"""
import logging
import re
from collections.abc import Iterable
from datetime import datetime

from ingest.ashare import clean, config
from ingest.ashare.net import HttpClient
from ingest.ashare.sources.base import BaseSource
from ingest.ashare.types import RawDoc
from ingest.ashare.util import CN_TZ, parse_datetime, sha1_hex

logger = logging.getLogger(__name__)

QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
PDF_BASE = "https://static.cninfo.com.cn/"
_CODE_RE = re.compile(r"^\d{6}$")

# 定期报告在巨潮上会拆成中文版、英文版、摘要、更正版多份：
# 「贵州茅台2024年年度报告(英文版)」实测 1398 个向量块，是中文正文的三倍多，
# 对中文问答几乎没有增量价值，却要付三倍存储与 embedding 费用。只留中文正文。
_REDUNDANT_PERIODIC = ("英文", "English", "(ENG", "摘要", "已取消")


def _is_redundant_periodic(title: str) -> bool:
    return any(key in title for key in _REDUNDANT_PERIODIC)


class CninfoAnnouncement(BaseSource):
    name = "cninfo"
    watermark_mode = "window"

    def __init__(self):
        self.http = HttpClient(qps=config.CNINFO_QPS)
        self._headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "https://www.cninfo.com.cn",
            "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "X-Requested-With": "XMLHttpRequest",
        }
        self.default_backfill_days = config.BACKFILL_DAYS["cninfo"]
        # 分类过滤能少翻一个数量级的公告：五年回填不筛分类，光列表就要翻 200 多万条
        self.categories = config.CNINFO_CATEGORIES.strip()

    def _query(self, page: int, se_date: str) -> dict:
        data = {
            "pageNum": page,
            "pageSize": 30,
            "column": config.CNINFO_COLUMN,
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": "",
            "secid": "",
            "category": self.categories,
            "trade": "",
            "seDate": se_date,
            "sortName": "",
            "sortType": "",
            "isHLtitle": "false",
        }
        payload = self.http.post_json(QUERY_URL, data=data, headers=self._headers)
        if not isinstance(payload, dict):
            raise RuntimeError(f"巨潮返回结构异常：{type(payload)}")
        return payload

    def fetch(
        self,
        since: datetime,
        until: datetime,
        limit: int | None = None,
        codes: Iterable[str] | None = None,
    ) -> Iterable[RawDoc]:
        code_filter = {str(c).zfill(6) for c in (codes or []) if str(c).strip()} or None
        se_date = f"{since.astimezone(CN_TZ):%Y-%m-%d}~{until.astimezone(CN_TZ):%Y-%m-%d}"
        yielded = 0
        page = 1

        while page <= config.CNINFO_MAX_PAGES:
            payload = self._query(page, se_date)
            items = payload.get("announcements") or []
            if not items:
                break

            for item in items:
                doc = self._to_doc(item)
                if doc is None:
                    continue
                # 公告的 announcementTime 多数是「当天 00:00」，按时间戳过滤会漏掉当日增量，
                # 因此只比日期；重复项由 source_id/指纹挡掉
                if doc.pub_time and doc.pub_time.date() < since.date():
                    continue
                if code_filter and not (set(doc.stock_codes) & code_filter):
                    continue
                yield doc
                yielded += 1
                if limit and yielded >= limit:
                    return

            total_pages = int(payload.get("totalpages") or 0)
            if total_pages and page >= total_pages:
                break
            if not payload.get("hasMore", True):
                break
            page += 1

        logger.info("巨潮窗口 %s 抓取 %d 条", se_date, yielded)

    def _to_doc(self, item: dict) -> RawDoc | None:
        code = str(item.get("secCode") or "").strip()
        if not _CODE_RE.match(code):
            return None
        title = clean.strip_html(str(item.get("announcementTitle") or ""))
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            return None

        pub = parse_datetime(item.get("announcementTime"))
        source_id = str(item.get("announcementId") or "").strip() or sha1_hex(code, title, str(pub))
        adjunct = str(item.get("adjunctUrl") or "").strip()
        pdf_url = (PDF_BASE + adjunct.lstrip("/")) if adjunct else ""

        source_type = clean.periodic_type(title) or "announcement"
        if source_type != "announcement" and _is_redundant_periodic(title):
            logger.debug("跳过定期报告的冗余版本：%s", title[:40])
            return None
        event_type = clean.announcement_event_type(title) or ("定期报告" if source_type != "announcement" else "公告")

        return RawDoc(
            source=self.name,
            source_id=source_id,
            source_type=source_type,
            title=title,
            pub_time=pub,
            url=pdf_url or f"https://www.cninfo.com.cn/new/disclosure/stock?stockCode={code}",
            pdf_url=pdf_url,
            stock_codes=[code],
            stock_names=[str(item.get("secName") or "").strip()],
            event_type=event_type,
            trust=1.0,
            extra={
                "column_id": item.get("columnId"),
                "adjunct_url": adjunct,
            },
        )
