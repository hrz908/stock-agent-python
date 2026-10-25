"""时间解析、哈希、日志等小工具。"""
import contextlib
import hashlib
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
)


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def parse_datetime(value) -> datetime | None:
    """宽松解析：akshare 会混用 datetime/date/Timestamp/字符串/毫秒时间戳。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif hasattr(value, "to_pydatetime"):  # pandas.Timestamp
        dt = value.to_pydatetime()
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts, CN_TZ)
    else:
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d{13}", text):
            return datetime.fromtimestamp(int(text) / 1000.0, CN_TZ)
        text = text.replace("T", " ").replace("Z", "")
        text = re.sub(r"\.\d+", "", text)
        dt = None
        for fmt in _DT_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=CN_TZ)
    return dt.astimezone(CN_TZ)


def parse_date_arg(text: str | None) -> datetime | None:
    return parse_datetime(text) if text else None


def to_day(value: datetime | None) -> str:
    return value.astimezone(CN_TZ).strftime("%Y-%m-%d") if value else ""


def sha1_hex(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()


def md5_hex(*parts: str) -> str:
    h = hashlib.md5()
    for p in parts:
        h.update((p or "").encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()


def reconfigure_stdio() -> None:
    """Windows 控制台默认 GBK/cp1252，中文日志与 argparse 帮助会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8")


def setup_logging(verbose: bool = False) -> None:
    reconfigure_stdio()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # akshare 底层 urllib3 的 DEBUG 会把每行请求头刷出来
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def split_csv(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,，\s]+", text or "") if x.strip()]


def cell(row, *names: str) -> str:
    """按候选列名取单元格，兼容 akshare 各版本改列名与 NaN。"""
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in ("nan", "nat", "none"):
            return text
    return ""
