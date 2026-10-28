"""巨潮资讯公告采集：公告 + 年报/半年报/季报，直接拿 PDF 原文链接。

选巨潮而非东财公告列表的原因：东财列表只给详情页 HTML，正文还得再猜一次内嵌；
巨潮的 hisAnnouncement/query 直接返回 adjunctUrl，拼接即得 static.cninfo.com.cn 上的 PDF，
少一层解析就少一类失败。

按股票过滤而不是翻全市场：不带 stock 参数时一次查询翻的是全市场公告（年报季单窗口
几百页），既慢又脆——翻页中途返回空页就静默截断，实测 500 只回填漏掉 3/4 的年报。
带上「code,orgId」后每批只回这几家公司的公告，一页打完，orgId 来自巨潮自带的代码表。
"""
import json
import logging
import re
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from ingest.ashare import clean, config
from ingest.ashare.net import HttpClient
from ingest.ashare.sources.base import BaseSource
from ingest.ashare.types import RawDoc
from ingest.ashare.util import CN_TZ, parse_datetime, sha1_hex

logger = logging.getLogger(__name__)

QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
PDF_BASE = "https://static.cninfo.com.cn/"
ORG_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
_CODE_RE = re.compile(r"^\d{6}$")
_PAGE_SIZE = 30
# 单批塞多少只股票：8 只 × 明文(年报+一季报+英文版+摘要) ≈ 30 条，基本一页打完
_BATCH_SIZE = 8
_ORG_TTL = 7 * 24 * 3600

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

    def _query(self, page: int, se_date: str, stock: str = "") -> dict:
        data = {
            "pageNum": page,
            "pageSize": _PAGE_SIZE,
            "column": config.CNINFO_COLUMN,
            "tabName": "fulltext",
            "plate": "",
            # 形如「600519,gssh0600519;000001,gssz0000001」：有它才是服务端按股票过滤
            "stock": stock,
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

    def _org_map(self) -> dict[str, str]:
        """代码 -> 巨潮 orgId（stock 参数必须要它），带 7 天本地缓存。

        取不到时返回空字典，调用方退化成「不带 stock 的全市场翻页」——慢但不会漏采。
        """
        path = Path(config.DATA_DIR) / "cninfo_orgs.json"
        cached: dict[str, str] = {}
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                cached = {str(k): str(v) for k, v in (payload.get("orgs") or {}).items()}
                if cached and time.time() - float(payload.get("ts", 0)) < _ORG_TTL:
                    return cached
            except (ValueError, OSError):
                cached = {}
        try:
            data = self.http.get_json(ORG_URL, headers={"Referer": "https://www.cninfo.com.cn/"})
            orgs = {
                str(item["code"]).zfill(6): str(item["orgId"])
                for item in (data or [])
                if isinstance(item, dict) and item.get("code") and item.get("orgId")
            }
            if orgs:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"ts": time.time(), "orgs": orgs}, ensure_ascii=False),
                               encoding="utf-8")
                tmp.replace(path)
                logger.info("巨潮代码表已刷新：%d 条 orgId", len(orgs))
                return orgs
        except Exception as exc:  # noqa: BLE001 - 拿不到就退回全市场翻页
            logger.warning("巨潮 orgId 表获取失败，退化为全市场翻页：%s", exc)
        return cached

    def _stock_batches(self, code_filter: set[str] | None) -> list[str]:
        """把股票列表切成若干批 stock 参数。

        不带 stock 时一次查询翻的是**全市场**：年报季单窗口几百页、十几分钟，
        翻页一断就静默少采一批（实测 500 只回填漏掉 3/4 的年报）。
        带上 code+orgId 后每批只回这一批公司的定期报告，一页就完。
        """
        if not code_filter:
            return [""]
        orgs = self._org_map()
        paired = [(c, orgs[c]) for c in sorted(code_filter) if c in orgs]
        if len(paired) < len(code_filter):
            missing = sorted(set(code_filter) - {c for c, _ in paired})
            logger.warning("巨潮代码表缺 %d 只的 orgId，本轮跳过：%s",
                           len(missing), ",".join(missing[:10]))
        batches = [
            ";".join(f"{c},{org}" for c, org in paired[i:i + _BATCH_SIZE])
            for i in range(0, len(paired), _BATCH_SIZE)
        ]
        # 一只都配不上时退回旧行为，宁可慢也别把窗口跑成空的
        return batches or [""]

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

        for stock in self._stock_batches(code_filter):
            page = 1
            while page <= config.CNINFO_MAX_PAGES:
                payload = self._query(page, se_date, stock)
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
                # 带 stock 过滤时 totalpages 常是 0，用「本页没填满」判断已经到底
                if len(items) < _PAGE_SIZE:
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
