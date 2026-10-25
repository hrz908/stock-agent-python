"""龙虎榜席位明细工具（数据源：akshare 东财龙虎榜）。

产品约定：资金流向卡片的龙虎榜只取三个量——席位、买入额、卖出额。

两级取数：
1. 主路：席位级。stock_lhb_stock_detail_date_em(symbol) 拿上榜日列表，
   再 stock_lhb_stock_detail_em(symbol, date, flag) 拿买卖两侧各前五席位；
   两侧表格各含同一批营业部的买入/卖出金额（元），按席位名合并。
2. 兜底：席位详情接口对空响应不设防（datacenter 限流时 akshare 直接
   TypeError 崩），此时退回 stock_lhb_detail_em 列表接口，给出
   「龙虎榜合计」一行（买入额/卖出额），仍满足三个量口径。

金额统一换算成万元；近期未上榜返回 ok=False 诚实降级。
"""
from datetime import datetime, timedelta

from langchain_core.tools import tool

from tools import call_tool_safe
from tools.market import digits, run_ds, text, wan

# 买卖两侧各最多回多少个席位（前端区块展示密度有限，5 个足够）
_MAX_SEATS_PER_SIDE = 5


async def _seat_level(sym: str) -> dict | None:
    """席位级明细；接口异常/无数据返回 None，由调用方走兜底。"""
    import akshare as ak

    try:
        dates = await run_ds(ak.stock_lhb_stock_detail_date_em, symbol=sym)
        if dates is None or dates.empty:
            return None
        day_raw = text(dates.iloc[0].get("交易日"))  # 倒序，首行即最近上榜日
        day = day_raw.replace("-", "")
        if not day:
            return None

        merged: dict = {}

        def _absorb(df, side: str, field: str) -> None:
            """把一侧席位表并入 merged：同一营业部买卖两表的金额按席位名合并。"""
            if df is None or df.empty:
                return
            for _, r in df.iterrows():
                seat = text(r.get("交易营业部名称"))
                if not seat:
                    continue
                item = merged.setdefault(seat, {"seat": seat, "buy": None, "sell": None})
                v = wan(r.get(f"{side}金额"))
                if item[field] is None:
                    item[field] = v

        buy_df = await run_ds(ak.stock_lhb_stock_detail_em, symbol=sym, date=day_raw, flag="买入")
        sell_df = await run_ds(ak.stock_lhb_stock_detail_em, symbol=sym, date=day_raw, flag="卖出")
        _absorb(buy_df, "买入", "buy")
        _absorb(sell_df, "卖出", "sell")
        if not merged:
            return None

        seats = list(merged.values())
        # 同侧按金额降序取前 5，三个量之外的维度一律不带
        buys = sorted([s for s in seats if s["buy"] is not None],
                      key=lambda s: s["buy"], reverse=True)[:_MAX_SEATS_PER_SIDE]
        sells = sorted([s for s in seats if s["sell"] is not None],
                       key=lambda s: s["sell"], reverse=True)[:_MAX_SEATS_PER_SIDE]
        rows, seen = [], set()
        for s in buys + sells:
            if s["seat"] in seen:
                continue
            seen.add(s["seat"])
            rows.append(s)
        if not rows:
            return None
        return {"trade_date": day, "rows": rows}
    except Exception:  # noqa: BLE001 - 席位接口限流/改版时退列表接口
        return None


async def _summary_level(sym: str) -> dict | None:
    """兜底：龙虎榜列表接口（较稳），给「合计」一行（买入额/卖出额，元->万元）。"""
    import akshare as ak

    # 兜底接口一次拉全市场逐日明细：窗口开 30 天会把上万行 DataFrame 读进内存，
    # 压到 5 天够覆盖「最近一次上榜」，长假后可临时调大
    end = datetime.now()
    start = end - timedelta(days=5)
    try:
        df = await run_ds(
            ak.stock_lhb_detail_em,
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
        )
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        return None
    hit = df[df["代码"].astype(str).str.zfill(6) == sym]
    if hit.empty:
        return None
    r = hit.iloc[0]  # 列表按上榜日倒序，首行即最近一次
    day = text(r.get("上榜日")).replace("-", "")
    buy = wan(r.get("龙虎榜买入额"))
    sell = wan(r.get("龙虎榜卖出额"))
    if not day or (buy is None and sell is None):
        return None
    return {"trade_date": day, "rows": [{"seat": "龙虎榜合计", "buy": buy, "sell": sell}]}


async def _fetch_lhb(code: str) -> dict:
    sym = digits(code)
    data = await _seat_level(sym)
    if data is None:
        data = await _summary_level(sym)
    if data is None:
        return {"ok": False, "error": "近期未上榜或暂无龙虎榜数据"}
    return {"ok": True, "data": data}


@tool
async def get_top_list(code: str) -> dict:
    """获取个股龙虎榜席位明细：只取每个席位的买入额、卖出额（共三个量：席位/买入/卖出）。
    适合回答「这只股票上龙虎榜了吗 / 哪些营业部在买卖」类问题。

    Args:
        code: 股票代码，如 "600519"。

    Returns:
        {"ok": True, "data": {trade_date, rows: [{seat, buy, sell}...]}} 成功
        （rows 买卖两侧按金额降序各最多 5 席，金额单位万元）；
        {"ok": False, "error": "..."} 失败（近期未上榜时返回明确说明）。
    """
    # 东财详情接口偶发瞬断（akshare 内部对空响应不设防），多给一次重试
    return await call_tool_safe(_fetch_lhb, code, retries=2)
