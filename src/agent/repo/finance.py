"""业务表只读访问：报告期财务指标 + 每日估值（迁移 009 / 014）。

只做两件事：按股票/报告期取结构化行，以及判断业务表是否还新鲜。
数值问答的取舍全在调用方（`agent/tools/fundamental.py`），这里不做降级决策。

为什么 freshness 看 `stock_data_sync_state.last_ok_at` 而不是「最新报告期距今多少天」：
报告期本身有披露滞后（Q3 到 10 月底才发完、年报到次年 4 月），按日期差判会周期性误判。
「同步任务最近一次成功是什么时候」才是真正决定数据可信度的信号，它一挂就该回落实时接口。

as-of 口径：只返回 announce_date <= 今天 的行，避免拿「还没披露」的数据回答历史问题；
announce_date 为空的行放行（东财偶发缺公告日，这些行必然是已发布的报告期）。
"""
from agent.storage.read import fetch_all

# 单只股票一次最多回多少个报告期：5 年 = 20 期，留一倍余量
_MAX_PERIODS = 40

_INDICATORS_SQL = """
SELECT report_date, report_type, announce_date,
       eps_yuan, bps_yuan, roe_pct, gross_margin_pct, net_margin_pct,
       debt_asset_pct, revenue_yoy_pct, netprofit_yoy_pct,
       revenue_yuan, netprofit_yuan, ocf_yuan
FROM stock_finance_indicator
WHERE code = %(code)s
  -- 用 NULL 而不是空串：''::date 会直接报 invalid input syntax，且 OR 不保证短路
  AND (%(period)s::date IS NULL OR report_date = %(period)s::date)
  AND (announce_date IS NULL OR announce_date <= CURRENT_DATE)
ORDER BY report_date DESC
LIMIT %(limit)s
"""

_VALUATION_SQL = """
SELECT trade_date, close_yuan, pe_static, pe_ttm, pb, ps_ttm,
       dv_ttm_pct, total_mv_yuan, circ_mv_yuan
FROM stock_valuation_daily
WHERE code = %(code)s
ORDER BY trade_date DESC
LIMIT %(limit)s
"""

_STATE_SQL = """
SELECT dataset, watermark, last_ok_at,
       (last_ok_at IS NOT NULL
        AND last_ok_at > now() - make_interval(hours => %(hours)s)) AS fresh
FROM stock_data_sync_state
WHERE dataset = %(dataset)s
"""


def _clamp_limit(limit, default: int = 40) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, _MAX_PERIODS))


async def sync_ok(dataset: str, max_age_hours: int = 48) -> bool:
    """同步任务最近一次成功是否在 max_age_hours 内。表不存在/没跑过一律 False。"""
    try:
        rows = await fetch_all(_STATE_SQL, {"dataset": dataset, "hours": max_age_hours})
    except Exception:
        # 业务表还没建（未执行 014）：当成「没有数据」，由调用方回落实时接口
        return False
    return bool(rows) and bool(rows[0].get("fresh"))


async def indicators(code: str, period: str = "", limit: int = 40) -> list[dict]:
    """按报告期倒序的财务指标；period 支持 YYYYMMDD / YYYY-MM-DD，留空取最近若干期。

    返回的 date 一律 ISO（与新浪接口的「日期」列同格式），金额单位为元。
    """
    params = {
        "code": code,
        # SQL 里直接和 date 比，先归一成 ISO；非法值退化成 NULL（=不过滤）而不是报错
        "period": _iso_period(period) or None,
        "limit": _clamp_limit(limit),
    }
    return await fetch_all(_INDICATORS_SQL, params)


async def valuation(code: str, limit: int = 30) -> list[dict]:
    """按交易日倒序的估值快照；金额单位为元（卡片契约的万元换算在工具层做）。"""
    return await fetch_all(_VALUATION_SQL, {"code": code, "limit": _clamp_limit(limit, 30)})


def _iso_period(period: str) -> str:
    """'20241231' / '2024-12-31' -> '2024-12-31'；其他输入返回空串（=不过滤）。"""
    text = (period or "").strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return ""
