"""工具层公共能力：安全 HTTP 调用 + Go 网关响应解包 + 进程内结果缓存。

call_tool_safe 统一封装「8s 超时 + 重试 1 次 + 失败降级」，
工具的网络/解析异常不会冒泡到图里，而是返回 {"ok": False, "error": ...}；
go_get 复用共享 httpx.AsyncClient（连接池 + keep-alive）并自解 Go 信封。

数据源：Go 网关（实时行情/搜索）、akshare（东财/新浪：估值/资金流/财报/资讯/龙虎榜/研报）、
baostock（历史日 K）、PG 主数据表（股票清单/行业/概念，只读），均免 token，工具全部无条件注册。
所有工具都是进程内 @tool 对象，由 LLM 的 tool_calls 直接驱动，无子进程、无协议层。
"""
import asyncio
import copy
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from config import (
    GO_SERVER_TOKEN,
    GO_SERVER_URL,
    TOOL_CACHE_MAX,
    TOOL_CACHE_TTL,
    TOOL_CACHE_TTL_FAILURE,
    TOOL_CACHE_TTL_REALTIME,
    TOOL_TIMEOUT,
)

# Go 网关统一响应体 { code, message, data } 里的成功码
_CODE_OK = 0

# 共享 HTTP 客户端：避免每次工具调用都新建连接
_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(TOOL_TIMEOUT, connect=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client


async def close_http_client() -> None:
    """进程退出时关闭共享客户端（可选调用）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def call_tool_safe(
    fn: Callable[..., Awaitable[Any]],
    *args,
    retries: int = 1,
    timeout: float = TOOL_TIMEOUT,
    **kwargs,
) -> dict:
    """执行一个异步工具函数，带超时与重试；失败返回降级结构而非抛异常。

    若 fn 本身已返回 {"ok": ...} 信封（如 go_get），直接透传不重复包裹。
    """
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
            if isinstance(result, dict) and "ok" in result:
                return result
            return {"ok": True, "data": result}
        except TimeoutError:
            last_err = "timeout"
        except Exception as e:  # noqa: BLE001 - 工具失败必须被吞掉转降级
            last_err = f"{type(e).__name__}: {e}"
        # 重试前短暂退避
        if attempt < retries:
            await asyncio.sleep(0.3)
    return {"ok": False, "error": last_err or "unknown"}


def _auth_headers() -> dict:
    h = {"Accept": "application/json"}
    if GO_SERVER_TOKEN:
        h["Authorization"] = f"Bearer {GO_SERVER_TOKEN}"
    return h


async def go_get(path: str, params: dict | None = None) -> dict:
    """GET Go 网关：拼 base_url、带鉴权头、解包 { code, data } 信封。

    返回 {"ok": True, "data": ...} 或 {"ok": False, "error": ...}；
    HTTP 层异常（非 2xx / 网络错误）仍抛出，由 call_tool_safe 兜底降级重试。
    """
    client = get_http_client()
    url = GO_SERVER_URL.rstrip("/") + path
    resp = await client.get(url, params=params or {}, headers=_auth_headers())
    resp.raise_for_status()
    data = resp.json()
    # 解 Go 信封 { code, message, data }
    if isinstance(data, dict) and "code" in data:
        if data.get("code") != _CODE_OK:
            return {"ok": False, "error": data.get("message", "unknown error")}
        return {"ok": True, "data": data.get("data")}
    return {"ok": True, "data": data}


# ===== 工具结果缓存（进程内 TTL + LRU）=====
# 取数工具全部只读、按日更新，同一只股票在几分钟内被反复问很常见（前端重试、多轮追问）；
# akshare 单次 1-3s，命中缓存直接把这段延迟省掉。
_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()

# 盘中随价格变的工具：缓存时长必须短，否则用户会看到旧价
_REALTIME_TOOLS = frozenset({"get_quote", "get_valuation", "get_money_flow", "get_peer_compare"})


def tool_cache_ttl(name: str) -> int:
    return TOOL_CACHE_TTL_REALTIME if name in _REALTIME_TOOLS else TOOL_CACHE_TTL


def _cache_key(name: str, args: dict) -> str:
    return name + ":" + json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)


def tool_cache_get(name: str, args: dict) -> dict | None:
    """命中返回结果副本，未命中/已过期返回 None。"""
    # 必须按工具档位判断：全局 TTL=0（关闭）时，盘口类仍按 30s 写入，
    # 用全局值先返回 None 会让这些缓存永远命中不了。
    # 失败缓存有独立 TTL：全局成功缓存关闭时，失败结论仍要能命中。
    if tool_cache_ttl(name) <= 0 and TOOL_CACHE_TTL_FAILURE <= 0:
        return None
    key = _cache_key(name, args)
    item = _cache.get(key)
    if item is None:
        return None
    expire_at, value = item
    if time.monotonic() >= expire_at:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return copy.deepcopy(value)


def tool_cache_put(name: str, args: dict, value: Any) -> None:
    """写入缓存；成功按工具档位、失败按短 TTL——数据源整站不可用时，
    同一轮里的重试不该反复等待同一个必挂的接口。"""
    if not isinstance(value, dict):
        return
    if value.get("ok"):
        ttl = tool_cache_ttl(name)
    else:
        ttl = TOOL_CACHE_TTL_FAILURE
    if ttl <= 0:
        return
    key = _cache_key(name, args)
    _cache[key] = (time.monotonic() + ttl, copy.deepcopy(value))
    _cache.move_to_end(key)
    while len(_cache) > max(1, TOOL_CACHE_MAX):
        _cache.popitem(last=False)


def tool_cache_clear() -> None:
    _cache.clear()


def to_ts_code(code: str) -> str:
    """股票代码 -> Tushare 风格 ts_code（600519 -> 600519.SH）。

    Tushare 数据源已下线，但前端卡片 note 与 LLM 上下文仍用这个展示格式；
    兼容 "600519" / "SH600519" / "600519.SH" 多种写法。
    """
    c = (code or "").strip().upper()
    if "." in c:
        return c
    for p in ("SH", "SZ", "BJ"):
        if c.startswith(p):
            c = c[len(p):]
            break
    if c.startswith("6"):
        return f"{c}.SH"
    if c.startswith(("0", "3")):
        return f"{c}.SZ"
    if c.startswith(("4", "8")):
        return f"{c}.BJ"
    return f"{c}.SH"


def take_latest(rows: Any, key: str = "trade_date", limit: int = 30) -> list:
    """按日期倒序取前 N 行（工具层统一的「只要最近若干条」出口）。

    数据源不保证返回顺序，这里显式按日期排序后再截断，
    保证拿到的永远是「最近 N 条」而不是「随机 N 条」。
    """
    if not isinstance(rows, list):
        return []

    def _sort_key(r: dict) -> str:
        v = r.get(key)
        return str(v) if v is not None else ""

    valid = [r for r in rows if isinstance(r, dict)]
    valid.sort(key=_sort_key, reverse=True)
    return valid[:limit] if limit and limit > 0 else valid


def get_tools() -> list:
    """返回全部可供 LLM 绑定的工具（惰性导入，避免包内循环依赖）。

    所有数据源（Go 网关 / akshare / baostock）均免 token，无条件注册；
    个别接口失败时由 call_tool_safe 降级，不影响其余工具。
    """
    from tools.compare import get_peer_compare
    from tools.fundamental import (
        get_financial_indicator,
        get_financial_report,
        get_valuation,
    )
    from tools.history import get_history_daily
    from tools.lhb import get_top_list
    from tools.moneyflow import get_money_flow
    from tools.news import get_stock_news
    from tools.peers import get_industry_peers
    from tools.quote import get_quote
    from tools.research import get_research_reports
    from tools.search import symbol_search

    return [
        get_quote, symbol_search, get_industry_peers, get_peer_compare,
        get_valuation, get_financial_indicator, get_financial_report,
        get_money_flow, get_stock_news, get_history_daily, get_top_list,
        get_research_reports,
    ]
