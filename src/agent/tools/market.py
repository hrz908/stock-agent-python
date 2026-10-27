"""数据源基础层：akshare + baostock 的统一异步包装。

两者都是同步、pandas 体系的 Python 库，接入约定：
- 一律经 run_ds / run_bs 丢进线程池执行，绝不阻塞 asyncio 事件循环；
- baostock 维护一条全局 socket 连接且非线程安全：懒登录 + 互斥锁串行化，
  登录失败在首次调用时抛错、由 call_tool_safe 降级；
- akshare 返回 DataFrame，列名是中文且值常见 numpy 标量 / NaN / 带逗号的字符串：
  所有出口必须经 num()/text() 归一成 JSON 安全的原生类型。

代码格式对照（换源最容易踩的坑）：baostock 用 sh.600519；东财 akshare 用 600519
（部分接口另要 market="sh"/"sz"）；前端卡片 note 展示用 600519.SH。
"""
import asyncio
import math
import threading
from functools import partial

# baostock 全局连接状态：懒登录 + 串行锁（库本身非线程安全）
_bs_lock = threading.Lock()
_bs_logged_in = False

# 浏览器 UA：akshare 用 requests 默认 UA 抓东财/新浪页面偶发被 WAF 直接断连，
# 进程级补一次默认 UA（不覆盖调用方显式 headers）
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _patch_requests_default_ua() -> None:
    import requests.sessions

    if getattr(requests.sessions.Session.request, "_ua_patched", False):
        return
    _orig = requests.sessions.Session.request

    def _request(self, method, url, **kw):
        headers = kw.get("headers") or {}
        headers.setdefault("User-Agent", BROWSER_UA)
        kw["headers"] = headers
        return _orig(self, method, url, **kw)

    _request._ua_patched = True
    requests.sessions.Session.request = _request


_patch_requests_default_ua()


def _ensure_bs_login() -> None:
    """在工作线程内登录 baostock（已登录则跳过）。"""
    global _bs_logged_in
    if _bs_logged_in:
        return
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    _bs_logged_in = True


def run_ds(fn, *args, **kwargs):
    """在线程池里跑一个 akshare 同步调用，返回 DataFrame。"""
    return asyncio.to_thread(partial(fn, *args, **kwargs))


def run_bs(fn, *args, **kwargs):
    """在线程池里跑一个 baostock 调用：先登录，再持锁串行执行。"""
    def _task():
        with _bs_lock:
            _ensure_bs_login()
            return fn()
    return asyncio.to_thread(_task)


# ===== 代码格式转换 =====

def digits(code: str) -> str:
    """提取 6 位裸代码："SH600519" / "600519.SH" / "sh.600519" -> "600519"。"""
    c = "".join(ch for ch in str(code or "") if ch.isdigit())
    return c.zfill(6)[-6:] if c else ""


def to_baostock_code(code: str) -> str:
    """600519 -> sh.600519（baostock 格式）。"""
    d = digits(code)
    if d.startswith("6"):
        return f"sh.{d}"
    if d.startswith(("4", "8")):
        return f"bj.{d}"
    return f"sz.{d}"


def to_market(code: str) -> str:
    """600519 -> sh（东财 stock_individual_fund_flow 的 market 参数）。"""
    d = digits(code)
    if d.startswith("6"):
        return "sh"
    if d.startswith(("4", "8")):
        return "bj"
    return "sz"


# ===== 数值 / 文本归一（akshare 出口统一走这里） =====

def num(v, digits_: int = 4):
    """DataFrame 单元格 -> JSON 安全的 float 或 None。

    兼容 numpy 标量、NaN / None、"8.02"、"1,234.56"、"--" 等形态。
    """
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
        s = str(v).strip().replace(",", "").replace("%", "")
        if s in ("", "--", "-", "nan", "None", "NaN"):
            return None
        f = float(s)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, digits_)
    except (TypeError, ValueError):
        return None


def text(v) -> str:
    """DataFrame 单元格 -> 干净字符串（NaN / None -> ""）。"""
    if v is None:
        return ""
    try:
        if isinstance(v, float) and math.isnan(v):
            return ""
    except TypeError:
        pass
    return str(v).strip()


def wan(v):
    """元 -> 万元（前端资金流向卡按万元口径换算回元），空值透传 None。"""
    f = num(v)
    return None if f is None else round(f / 1e4, 2)
