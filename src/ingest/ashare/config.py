"""A股文本语料入库配置。全部可被环境变量覆盖。

注意三点：
- ASHARE_EMBED_DIM 必须与 chunks.embedding 的维度、agent 的在线 embedding 维度一致；
- ASHARE_MODEL_PATH 指向本地模型目录，容器内挂载只读，确保断网可跑；
- 采集限速按「每秒请求数」给，反爬策略一改这里就能降速，不必改代码。
"""
import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


DATABASE_URL = (
    os.getenv("ASHARE_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or "postgresql://postgres:postgres@localhost:5432/stock_agent"
)

# ---- 本地目录 ----
DATA_DIR = Path(os.getenv("ASHARE_DATA_DIR", "./data/ashare"))
SPOOL_DIR = Path(os.getenv("ASHARE_SPOOL_DIR", str(DATA_DIR / "spool")))
PDF_CACHE_DIR = Path(os.getenv("ASHARE_PDF_DIR", str(DATA_DIR / "pdf")))
KEEP_PDF = _bool("ASHARE_KEEP_PDF", False)
MAX_PDF_MB = _int("ASHARE_MAX_PDF_MB", 80)

# ---- 嵌入模型 ----
EMBED_MODEL = os.getenv("ASHARE_EMBED_MODEL", "Qwen/Qwen3-Embedding-4B")
# 指向本地目录时不再访问 HuggingFace；离线部署必须设置
EMBED_MODEL_PATH = os.getenv("ASHARE_MODEL_PATH", "")
EMBED_API_BASE = os.getenv("ASHARE_EMBED_API_BASE", "")
EMBED_API_KEY = os.getenv("ASHARE_EMBED_API_KEY", "")
EMBED_DIM = _int("ASHARE_EMBED_DIM", 1536)
# 本地 4B 模型单批 8 条即可；显存/内存紧张时继续调小，API 后端可调大
EMBED_BATCH = _int("ASHARE_EMBED_BATCH", 8)
EMBED_MAX_SEQ = _int("ASHARE_EMBED_MAX_SEQ", 1024)
# 模型原生维度 > 目标维度时按 MRL 截断并重新归一化（Qwen3 系列支持）
EMBED_TRUNCATE = _bool("ASHARE_EMBED_TRUNCATE", True)
DEVICE = os.getenv("ASHARE_DEVICE", "auto")
EMBED_RETRIES = _int("ASHARE_EMBED_RETRIES", 2)

# ---- 分块 ----
# 中文正文按字符切：450 字 ≈ Qwen3 的 350~450 token，单块信息密度与召回精度更平衡
CHUNK_MAX_CHARS = _int("ASHARE_CHUNK_MAX_CHARS", 450)
CHUNK_MIN_CHARS = _int("ASHARE_CHUNK_MIN_CHARS", 60)
CHUNK_OVERLAP_CHARS = _int("ASHARE_CHUNK_OVERLAP_CHARS", 80)

# ---- 质量门禁 ----
MIN_QUALITY = _float("ASHARE_MIN_QUALITY", 0.45)
NEAR_DUP_HAMMING = _int("ASHARE_NEAR_DUP_HAMMING", 3)

# ---- 采集 ----
HTTP_TIMEOUT = _float("ASHARE_HTTP_TIMEOUT", 20.0)
HTTP_RETRIES = _int("ASHARE_HTTP_RETRIES", 3)
CNINFO_QPS = _float("ASHARE_CNINFO_QPS", 1.0)
EASTMONEY_QPS = _float("ASHARE_EASTMONEY_QPS", 2.0)
CNINFO_COLUMN = os.getenv("ASHARE_CNINFO_COLUMN", "szse")
CNINFO_MAX_PAGES = _int("ASHARE_CNINFO_MAX_PAGES", 800)
# whitelist：只抓重要公告正文（回购/重组/业绩预告等）；all：全部；none：只入库标题
ANNOUNCEMENT_PDF_MODE = os.getenv("ASHARE_ANNOUNCEMENT_PDF_MODE", "whitelist").lower()
FETCH_REPORT_PDF = _bool("ASHARE_FETCH_REPORT_PDF", True)
# 单次运行最多解析多少份 PDF（定期报告不计入：一天就那几份，且必须入库正文）
PDF_MAX_PER_RUN = _int("ASHARE_PDF_MAX_PER_RUN", 300)
REPORT_QTYPES = os.getenv("ASHARE_REPORT_QTYPES", "0,1,2,3")
REPORT_PAGE_SIZE = _int("ASHARE_REPORT_PAGE_SIZE", 50)
REPORT_MAX_PAGES = _int("ASHARE_REPORT_MAX_PAGES", 40)

# ---- 主数据（股票清单/行业/概念）----
# 代码表少于该条数时不做退市判定：上游偶发只返回部分市场，照单全收会把半个市场标死
MASTER_MIN_CODES = _int("ASHARE_MASTER_MIN_CODES", 4000)

# 国务院政策文件库：gw=国务院文件，bm=部门文件
GOV_TYPES = os.getenv("ASHARE_GOV_TYPES", "gw,bm")
GOV_QPS = _float("ASHARE_GOV_QPS", 1.0)
GOV_MAX_PAGES = _int("ASHARE_GOV_MAX_PAGES", 20)
# 列表只给一两句摘要；正文才是政策条款，默认再抓一次详情页
GOV_FETCH_ARTICLE = _bool("ASHARE_GOV_FETCH_ARTICLE", True)

CLS_SYMBOL = os.getenv("ASHARE_CLS_SYMBOL", "全部")
NEWS_CODES = os.getenv("ASHARE_NEWS_CODES", "")  # 逗号分隔；留空则只跑市场级快讯
NEWS_MAX_CODES = _int("ASHARE_NEWS_MAX_CODES", 0)

# 窗口型源回看天数（公告/研报按日期区间查，增量时向前多取几天防迟到）
BACKFILL_DAYS = {
    "cninfo": _int("ASHARE_CNINFO_BACKFILL_DAYS", 3),
    "em-report": _int("ASHARE_REPORT_BACKFILL_DAYS", 7),
    "cls": _int("ASHARE_CLS_BACKFILL_DAYS", 1),
    "ths": _int("ASHARE_THS_BACKFILL_DAYS", 1),
    "em-global": _int("ASHARE_EM_GLOBAL_BACKFILL_DAYS", 1),
    "em-news": _int("ASHARE_NEWS_BACKFILL_DAYS", 1),
    "gov-policy": _int("ASHARE_GOV_BACKFILL_DAYS", 7),
}
# 水位回退量：定时任务晚到/源侧补发都靠这段重叠兜住，重复项由指纹去重。
# 公告是实时发布的，2h 足够；拿 24h 会让 30 分钟一次的 daemon 每次都重扫一整天。
WINDOW_OVERLAP_HOURS = _int("ASHARE_WINDOW_OVERLAP_HOURS", 2)
# 研报发布日期可能被回填一两天，窗口给宽一些（量小，代价可控）
REPORT_OVERLAP_HOURS = _int("ASHARE_REPORT_OVERLAP_HOURS", 72)
LATEST_OVERLAP_HOURS = _int("ASHARE_LATEST_OVERLAP_HOURS", 6)

# ---- 守护进程 ----
DAEMON_INTERVALS = {
    "cls": _int("ASHARE_CLS_INTERVAL", 300),
    "ths": _int("ASHARE_THS_INTERVAL", 300),
    "em-global": _int("ASHARE_EM_GLOBAL_INTERVAL", 600),
    "gov-policy": _int("ASHARE_GOV_INTERVAL", 3600),
    "cninfo": _int("ASHARE_CNINFO_INTERVAL", 1800),
    "em-report": _int("ASHARE_REPORT_INTERVAL", 7200),
    "em-news": _int("ASHARE_NEWS_INTERVAL", 3600),
}
DAEMON_ORDER = ("cls", "ths", "em-global", "gov-policy", "cninfo", "em-report", "em-news")
