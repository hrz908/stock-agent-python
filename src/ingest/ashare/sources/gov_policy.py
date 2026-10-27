"""国务院政策文件库采集：国务院文件（gw）+ 部门文件（bm）。

接口是 gov.cn 搜索页自用的公开 JSON（sousuo.www.gov.cn/search-gov/data），
按发布时间倒序，不需要登录。列表自带 summary，但只有一两句，正文要再抓一次
详情页——政策的价值在全文条款，摘要向量化等于只存了个标题。
"""
import logging
import re
import urllib.parse
from collections.abc import Iterable
from datetime import datetime

from ingest.ashare import clean, config
from ingest.ashare.net import HttpClient
from ingest.ashare.sources.base import BaseSource
from ingest.ashare.types import RawDoc
from ingest.ashare.util import parse_datetime

logger = logging.getLogger(__name__)

API_URL = "https://sousuo.www.gov.cn/search-gov/data"
_TYPE_SUFFIX = {"gw": "zhengcelibrary_gw", "bm": "zhengcelibrary_bm"}
_TITLE_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    return clean.normalize(_TITLE_TAG_RE.sub("", text or ""))


class GovPolicy(BaseSource):
    name = "gov-policy"
    watermark_mode = "latest"

    def __init__(self):
        self.http = HttpClient(qps=config.GOV_QPS)
        self.types = [t.strip() for t in config.GOV_TYPES.split(",") if t.strip() in _TYPE_SUFFIX]
        if not self.types:
            raise RuntimeError("ASHARE_GOV_TYPES 无有效值，只能是 gw / bm")
        self.default_backfill_days = config.BACKFILL_DAYS["gov-policy"]

    def _query(self, kind: str, page: int) -> dict:
        params = {
            "t": _TYPE_SUFFIX[kind],
            "q": "",
            "timetype": "timeqb",
            "mintime": "",
            "maxtime": "",
            "sort": "pubtime",
            "sortType": "1",
            "searchfield": "",
            "p": page,
            "n": 20,
            "type": "gwyzcwjk",
        }
        payload = self.http.get_json(
            f"{API_URL}?{urllib.parse.urlencode(params)}",
            headers={"Referer": "https://www.gov.cn/zhengce/zhengceku/"},
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"政策库返回结构异常：{type(payload)}")
        return payload.get("searchVO") or {}

    def fetch(
        self,
        since: datetime,
        until: datetime,
        limit: int | None = None,
        codes: Iterable[str] | None = None,
    ) -> Iterable[RawDoc]:
        yielded = 0
        seen: set[str] = set()

        for kind in self.types:
            for page in range(1, config.GOV_MAX_PAGES + 1):
                data = self._query(kind, page)
                items = data.get("listVO") or []
                if not items:
                    break
                for item in items:
                    pub = parse_datetime(item.get("pubtime") or item.get("pubtimeStr"))
                    if pub and pub < since:
                        continue
                    doc = self._to_doc(item, pub)
                    if doc is None or doc.source_id in seen:
                        continue
                    seen.add(doc.source_id)
                    yield doc
                    yielded += 1
                    if limit and yielded >= limit:
                        return
                if len(items) < 20 or page >= int(data.get("totalpage") or 0):
                    break
            logger.info("政策库 %s 抓取完成", kind)

        logger.info("政策库共抓取 %d 条", yielded)

    def _to_doc(self, item: dict, pub: datetime | None) -> RawDoc | None:
        title = _plain(str(item.get("title") or ""))
        if not title:
            return None
        url = str(item.get("url") or "").strip()
        body = _plain(str(item.get("summary") or ""))
        if config.GOV_FETCH_ARTICLE and url:
            article = self._fetch_article(url)
            if article:
                body = article

        return RawDoc(
            source=self.name,
            source_id=str(item.get("id") or title),
            source_type="industry_policy",
            title=title,
            pub_time=pub,
            url=url,
            content=body,
            org=_plain(str(item.get("puborg") or "")),
            event_type="政策文件",
            trust=1.0,
            extra={
                "pcode": item.get("pcode"),          # 发文字号
                "childtype": item.get("childtype"),  # 政策主题分类
                "index": item.get("index"),
            },
        )

    def _fetch_article(self, url: str) -> str:
        try:
            resp = self.http.request("GET", url, headers={"Referer": "https://www.gov.cn/"})
            # 页面声明 utf-8，但响应头常不带 charset；用 bytes 解码避免 requests 猜成 latin-1
            html = resp.content.decode("utf-8", errors="replace")
        except Exception as exc:  # 详情页失败退回摘要，不丢整条政策
            logger.warning("政策正文抓取失败（%s）：%s", url, exc)
            return ""

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return ""

        # 政府网详情页有未闭合标签，lxml 会把整块正文丢掉；html.parser 才能找到 UCAP-CONTENT
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("div#UCAP-CONTENT") or soup.select_one("div.pages_content")
        if node is None:
            node = BeautifulSoup(html, "lxml").select_one("div#UCAP-CONTENT")
        if node is None:
            return ""
        # 正文里的附件下载/相关链接是导航噪声，整块去掉
        for tag in node.select("script, style, .attach, .related"):
            tag.decompose()
        return clean.normalize(node.get_text("\n"))
