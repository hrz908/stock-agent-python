"""A股代码表：本地缓存 + akshare 刷新，供个股新闻等按股票采集的源使用。"""
import json
import logging
import time
from pathlib import Path

from ingest.ashare import config

logger = logging.getLogger(__name__)

_CACHE_TTL = 7 * 24 * 3600


def _cache_path() -> Path:
    return Path(config.DATA_DIR) / "universe.json"


def load_universe(force: bool = False) -> dict[str, str]:
    """返回 {6 位代码: 简称}。刷新失败时回落到旧缓存，不阻塞其它源。"""
    path = _cache_path()
    cached: dict[str, str] = {}
    fresh = False
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached = payload.get("codes") or {}
            fresh = (time.time() - float(payload.get("ts", 0))) < _CACHE_TTL
        except (ValueError, OSError):
            cached = {}

    if cached and fresh and not force:
        return cached

    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        code_col = next((c for c in df.columns if str(c).lower() in ("code", "代码", "证券代码")), df.columns[0])
        name_col = next((c for c in df.columns if str(c).lower() in ("name", "名称", "证券简称")), df.columns[1])
        codes = {
            str(row[code_col]).zfill(6): str(row[name_col]).strip()
            for _, row in df.iterrows()
            if str(row[code_col]).strip().isdigit()
        }
        if codes:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": time.time(), "codes": codes}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            logger.info("A股代码表已刷新：%d 只", len(codes))
            return codes
    except Exception as exc:
        logger.warning("A股代码表刷新失败，使用旧缓存（%d 条）：%s", len(cached), exc)

    if cached:
        return cached
    raise RuntimeError("无法获取 A股代码表：akshare 不可用且本地无缓存")
