"""HTTP 客户端：按域名限速 + 指数退避重试 + PDF 落盘校验。"""
import hashlib
import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from ashare import config

logger = logging.getLogger(__name__)

_UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
)


class HttpClient:
    """同一实例内共享连接池；qps 是「单域名每秒请求数」上限。"""

    def __init__(
        self,
        qps: float | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ):
        self.qps = qps if qps is not None else config.EASTMONEY_QPS
        self.timeout = timeout if timeout is not None else config.HTTP_TIMEOUT
        self.retries = retries if retries is not None else config.HTTP_RETRIES
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(_UA_POOL),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._last_call: dict[str, float] = {}

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        min_interval = 1.0 / self.qps if self.qps > 0 else 0.0
        last = self._last_call.get(host, 0.0)
        delay = min_interval - (time.monotonic() - last)
        if delay > 0:
            time.sleep(delay)
        self._last_call[host] = time.monotonic()

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._wait(url)
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("请求失败(%s/%s)：%s - %s", attempt + 1, self.retries, url, exc)
            else:
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = RuntimeError(f"HTTP {resp.status_code}")
                    logger.warning("上游繁忙(%s/%s)：%s - %s", attempt + 1, self.retries, url, resp.status_code)
                elif resp.status_code >= 400:
                    resp.raise_for_status()
                else:
                    return resp
            time.sleep(0.6 * (2 ** attempt) + random.random() * 0.4)
        raise RuntimeError(f"请求重试 {self.retries} 次仍失败：{url} - {last_error}")

    def get_json(self, url: str, **kwargs) -> dict | list:
        resp = self.request("GET", url, **kwargs)
        return _loads_jsonp(resp.text)

    def post_json(self, url: str, data: dict, **kwargs) -> dict | list:
        resp = self.request("POST", url, data=data, **kwargs)
        return _loads_jsonp(resp.text)


def _loads_jsonp(text: str):
    """东财/巨潮部分接口返回 JSONP 或带 BOM 的 JSON。"""
    import json

    text = (text or "").lstrip("\ufeff").strip()
    if not text:
        raise ValueError("空响应")
    if text[0] in "[{":
        return json.loads(text)
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start < 0 or end <= start:
        raise ValueError(f"无法解析响应：{text[:120]}")
    return json.loads(text[start:end + 1])


def download_pdf(url: str, cache_dir: Path, client: HttpClient | None = None) -> Path:
    """下载 PDF 到缓存目录；非 PDF（常见于反爬返回的 HTML）直接报错，避免脏文本入库。"""
    client = client or HttpClient()
    cache_dir.mkdir(parents=True, exist_ok=True)
    resp = client.request("GET", url, stream=True)
    # 同名 PDF（不同公告 PDF 的文件名常都是 finalpage.PDF）必须加 URL 哈希前缀，否则并发互踩
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    name = re.sub(r"[^\w.-]+", "_", url.rsplit("/", 1)[-1])[:70] or "doc.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    path = cache_dir / f"{digest}-{name}"
    tmp = cache_dir / f"{path.name}.{time.time_ns()}.part"

    size = 0
    limit = config.MAX_PDF_MB * 1024 * 1024
    try:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "pdf" not in content_type and "octet-stream" not in content_type:
            logger.warning("非 PDF 响应(%s)：%s", content_type, url)

        with tmp.open("wb") as fh:
            for block in resp.iter_content(64 * 1024):
                if not block:
                    continue
                size += len(block)
                if size > limit:
                    raise RuntimeError(f"PDF 超过 {config.MAX_PDF_MB}MB：{url}")
                fh.write(block)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        resp.close()

    with tmp.open("rb") as fh:
        magic = fh.read(5)
    if magic != b"%PDF-":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"响应不是合法 PDF：{url}")
    tmp.replace(path)
    logger.debug("PDF 已下载：%s (%.1fMB)", path.name, size / 1024 / 1024)
    return path
