"""同业对比工具（数据源：Go 网关行情快照，腾讯当日数据）。

产品约定：同业对比只取当日数据。五个对比维度全部来自一次
`GET /api/v1/stock/quote`（腾讯当日快照）：最新价、当日涨跌幅、
市盈率 PE(TTM)、市净率 PB、总市值——不碰任何历史序列与财务接口，
不消耗 Tushare 配额，快照在网关还有 30s Redis 缓存。

peers 优先由模型按行业判断给出（不知道代码时先调 symbol_search / get_industry_peers），
最多 2 家同行（前端对比卡固定「本股 + 2 家」三列）；留空时用主数据表按同一东财行业补，
只是兜底——按代码序取的前两家不一定是最合适的可比公司。
"""
import asyncio

from langchain_core.tools import tool

from tools import call_tool_safe, go_get, to_ts_code
from tools.market import digits

# 前端 compare 卡固定三列：本股 + 最多两家同行
_MAX_PEERS = 2


def _split_peers(peers: str, exclude: str = "") -> list:
    """"000858,600809" -> ["000858", "600809"]：归一成 6 位裸码、去重、排除本股。

    网关只认「6 位」或「前缀+6 位」，"000858.SZ" 这类点号写法会被直接拒掉；
    同一只股票的 "600519" / "SH600519" / "600519.SH" 也必须在进对比卡前收敛成一只，
    否则前端那张固定三列的卡里会出现重复的同一家公司。
    """
    me = digits(exclude)
    out: list[str] = []
    for p in (peers or "").replace("，", ",").split(","):
        c = digits(p)
        if not c or c == me or c in out:
            continue
        out.append(c)
        if len(out) >= _MAX_PEERS:
            break
    return out


async def _fetch_one(code: str) -> dict:
    """拉一只股票的当日快照并折成对比卡的 item 结构；失败降级为带 error 的项。"""
    res = await call_tool_safe(go_get, "/api/v1/stock/quote", {"code": code})
    if not res.get("ok"):
        return {"code": code, "error": str(res.get("error") or "quote failed")}
    d = res.get("data") or {}
    def _f(v):
        try:
            return round(float(v), 4) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return {
        "code": d.get("code") or code,
        "name": d.get("name") or code,
        # 前端 compare 卡的五个维度（详见 components/finance/compare-card）
        "price": _f(d.get("price")),
        "changePct": _f(d.get("change_percent")),
        "pe": _f(d.get("pe")),
        "pb": _f(d.get("pb")),
        "totalMv": _f(d.get("total_mv")),
    }


async def _auto_peers(code: str) -> list[str]:
    """peers 留空时按同一东财行业补候选；主数据不可用则返回空（退化为只比本股）。"""
    try:
        from repo.stock import industry_peers

        data = await industry_peers(digits(code), limit=_MAX_PEERS)
    except Exception:  # noqa: BLE001 - 兜底失败不能把对比工具一起拖挂
        return []
    return [p["code"] for p in (data.get("peers") or []) if p.get("code")][:_MAX_PEERS]


@tool
async def get_peer_compare(code: str, peers: str = "") -> dict:
    """同业对比：本股 + 最多 2 家同行，仅取当日行情快照对比
    最新价、涨跌幅、市盈率 PE(TTM)、市净率 PB、总市值。

    Args:
        code: 本股代码，如 "600519" 或 "SH600519"。
        peers: 同行代码，逗号分隔（如 "000858,600809"），最多 2 家；
               只认其中的代码部分，与本股重复的会被丢掉；
               只有名称不知道代码时先调 symbol_search；
               想挑规模相近的同行时先调 get_industry_peers 再传进来——
               留空会自动按同行业补两家，但那只是兜底，不一定是最合适的比较对象。

    Returns:
        {"ok": True, "data": {items: [{name, price, changePct, pe, pb, totalMv}...],
        ts_code, peers_source}}；
        成功（items 首位是本股）；单只失败时该项带 error，不影响其余标的；
        本股代码本身非法时 {"ok": False, "error": ...}，不发行情请求。
    """
    me = digits(code)
    if not me:
        return {"ok": False, "error": "股票代码无效"}
    peers_list = _split_peers(peers, me)
    codes = [me, *peers_list]
    source = "explicit" if peers_list else "industry"
    if not peers_list:
        codes += await _auto_peers(me)
    if len(codes) == 1:
        source = "none"

    items = await asyncio.gather(*[_fetch_one(c) for c in codes])
    ok_items = [it for it in items if "error" not in it]
    if not ok_items:
        return {"ok": False, "error": "行情快照全部获取失败，无法对比"}
    return {
        "ok": True,
        "data": {"items": items, "ts_code": to_ts_code(me), "peers_source": source},
    }
