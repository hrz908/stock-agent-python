"""问句模板与市场阶段。

市场阶段决定语气：盘后还问「今天为什么涨」是语境错位，用户第一眼就会
觉得这批建议是提前写死的。

写模板的两条硬约束（2026-10-26 按线上反馈重写过，别随手加新模板）：
1. 只代入 {name}/{code} 就要通顺——模板不能假设「今天涨了」「刚出财报」这类前置事实；
2. 必须能被现有取数工具答出来（行情/量能、资金四档、估值、财报、近 15 日走势、
   当日资讯、研报、龙虎榜、同业对比）。北向持仓、筹码分布、周线/分钟线、
   买卖点建议都没有数据源，写进去只会答「取不到」。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from enum import IntEnum

from gateway.services.quickask.models import Ask, Candidate, Template

CST = timezone(timedelta(hours=8))


class Phase(IntEnum):
    PRE_OPEN = 0
    TRADING = 1
    AFTER_CLOSE = 2
    WEEKEND = 3


def phase_at(now: datetime) -> Phase:
    moment = now.astimezone(CST)
    if moment.weekday() >= 5:
        return Phase.WEEKEND
    minutes = moment.hour * 60 + moment.minute
    if minutes < 9 * 60 + 15:
        return Phase.PRE_OPEN
    if minutes < 15 * 60:
        return Phase.TRADING
    return Phase.AFTER_CLOSE


# 盘中异动（涨跌幅超 5%）：只问当下就能答的盘面
SNAPSHOT_TEMPLATES: list[Template] = [
    Template(
        "盘面",
        "{name}现在什么价？",
        "{name}（{code}）现在什么价？涨跌幅、成交额和换手率分别是多少",
    ),
    Template(
        "盘面",
        "{name}今天这波涨跌是什么原因？",
        "{name}（{code}）今天这波涨跌是什么原因？结合量价和今日消息说",
    ),
    Template(
        "盘面",
        "{name}今天的最高价和最低价到过多少？",
        "{name}（{code}）今天的开盘价、最高价和最低价分别是多少",
    ),
    Template(
        "盘面",
        "{name}今天的成交额和换手率是多少？",
        "{name}（{code}）今天的成交额、换手率和量比分别是多少",
    ),
    Template(
        "盘面",
        "{name}现在的总市值是多少？",
        "{name}（{code}）现在的最新价、涨跌幅和总市值分别是多少",
    ),
]

# 休市（盘后 / 周末 / 盘前）不能复用「今天为什么涨」，改成复盘
CLOSED_SNAPSHOT_TEMPLATES: list[Template] = [
    Template(
        "盘面",
        "复盘一下{name}最近的走势",
        "{name}（{code}）最近10个交易日的走势和量能怎么样",
    ),
    Template(
        "盘面",
        "{name}现在离近期高点还有多远？",
        "{name}（{code}）近15个交易日的最高价和最低价是多少？现在处在区间什么位置",
    ),
    Template(
        "盘面",
        "{name}最近成交量的变化怎么样？",
        "{name}（{code}）最近10个交易日的成交量有什么变化",
    ),
    Template(
        "盘面",
        "{name}最近有没有放量？",
        "{name}（{code}）最近几个交易日有放量吗？量比和换手率是什么水平",
    ),
]

CAPITAL_TEMPLATES: list[Template] = [
    Template(
        "资金",
        "{name}最近主力资金在流入还是流出？",
        "{name}（{code}）最近5个交易日主力资金净流入情况怎么样",
    ),
    Template(
        "资金",
        "{name}最近的超大单和大单在做什么？",
        "{name}（{code}）最新一个交易日的超大单、大单、中单、小单分别净流入多少",
    ),
    Template(
        "资金",
        "{name}最近上过龙虎榜吗？",
        "{name}（{code}）最近上过龙虎榜吗？上榜的话席位买卖情况怎么样",
    ),
    Template(
        "资金",
        "{name}的主力资金是连续流入吗？",
        "{name}（{code}）最近主力资金是连续净流入还是流出？连续了几天",
    ),
]

FUNDAMENTAL_TEMPLATES: list[Template] = [
    Template(
        "基本面",
        "{name}最新财报的营收和净利润是多少？",
        "{name}（{code}）最新一期财报的营业收入和净利润分别是多少？同比变化如何",
    ),
    Template(
        "基本面",
        "{name}的毛利率和ROE是什么水平？",
        "{name}（{code}）最新的毛利率、净利率和ROE分别是多少",
    ),
    Template(
        "基本面",
        "{name}最近有机构研报吗？",
        "{name}（{code}）最近有哪些机构研报？评级和目标价是多少",
    ),
]

TECHNICAL_TEMPLATES: list[Template] = [
    Template(
        "技术",
        "{name}现在的关键支撑位和阻力位在哪？",
        "{name}（{code}）现在的关键支撑位和阻力位在哪？结合最近10个交易日的量价说",
    ),
    Template(
        "技术",
        "{name}最近15个交易日的日K线怎么走的？",
        "{name}（{code}）最近15个交易日的日K线怎么走的？量能有什么变化",
    ),
]

RISK_TEMPLATES: list[Template] = [
    Template(
        "风险",
        "{name}最近有哪些需要留意的风险？",
        "{name}（{code}）从最近的消息和财务数据看，有哪些需要留意的风险",
    ),
    Template(
        "风险",
        "{name}最近的股价波动主要受什么影响？",
        "{name}（{code}）最近股价波动主要受什么影响？资金面和消息面各说一点",
    ),
]

WATCH_TEMPLATES: list[Template] = [
    Template(
        "观察",
        "{name}最近有哪些值得关注的信号？",
        "{name}（{code}）最近有哪些值得关注的信号？量价、资金和消息面各说一句",
    ),
    Template(
        "观察",
        "{name}最近有需要跟踪的公告或事件吗？",
        "{name}（{code}）最近有需要跟踪的公告或事件吗？有的话说清是什么",
    ),
]

TAG_KEY_PREFIX = "tag:"
TPL_KEY_PREFIX = "tpl:"


def _tag_key(tag: str) -> str:
    return TAG_KEY_PREFIX + tag


def _tpl_key(template: Template) -> str:
    return TPL_KEY_PREFIX + template.display


def eligible_dims(candidate: Candidate, phase: Phase) -> list[list[Template]]:
    """先看股票状态，再看市场阶段，给出此刻可用的维度（顺序即优先级）。

    状态决定「问什么」：大涨大跌问盘面、放量问资金、盘后问基本面，
    横盘才轮到技术 / 观察 / 风险这类没有时效性的维度。
    """
    if abs(candidate.change_pct) > 5:
        if phase == Phase.TRADING:
            return [SNAPSHOT_TEMPLATES]
        return [CLOSED_SNAPSHOT_TEMPLATES]
    if candidate.volume_ratio > 2:
        return [CAPITAL_TEMPLATES]
    # 财报季暂不细分：网关侧没有财报日历，盘后统一偏基本面
    if phase in (Phase.AFTER_CLOSE, Phase.WEEKEND):
        return [FUNDAMENTAL_TEMPLATES, RISK_TEMPLATES, WATCH_TEMPLATES]
    return [TECHNICAL_TEMPLATES, WATCH_TEMPLATES, RISK_TEMPLATES]


def _rand_tpl(group: list[Template]) -> Template:
    return group[random.randrange(len(group))]


def _pick_least_used(group: list[Template], used: dict[str, int]) -> Template:
    """组内挑本批用得最少的模板，同分随机。"""
    lowest = -1
    pool: list[Template] = []
    for template in group:
        count = used.get(_tpl_key(template), 0)
        if lowest < 0 or count < lowest:
            lowest = count
            pool = [template]
        elif count == lowest:
            pool.append(template)
    return _rand_tpl(pool)


def _pick_template_for(
    candidate: Candidate, phase: Phase, used: dict[str, int]
) -> Template:
    """在一批里挑「当前用得最少」的维度，同分随机；组内再优先挑没用过的模板。

    一次给四条建议，若每条都走首选维度，盘后会四条全是基本面，像复读机；
    维度打散之后仍可能抽到同一句，所以模板也要计一次。
    两者都只是批内偏好，不会越过状态约束（大涨的票仍然只问盘面）。
    """
    dims = eligible_dims(candidate, phase)
    best: list[list[Template]] = []
    best_count = -1
    for group in dims:
        count = used.get(_tag_key(group[0].tag), 0)
        if best_count < 0 or count < best_count:
            best_count = count
            best = [group]
        elif count == best_count:
            best.append(group)

    chosen = best[random.randrange(len(best))]
    used[_tag_key(chosen[0].tag)] = used.get(_tag_key(chosen[0].tag), 0) + 1
    template = _pick_least_used(chosen, used)
    used[_tpl_key(template)] = used.get(_tpl_key(template), 0) + 1
    return template


def digits_of(code: str) -> str:
    """取 6 位裸码：对外统一裸码，小程序与 Agent 两边都认这种写法。"""
    text = code or ""
    last_non_digit = -1
    for index, ch in enumerate(text):
        if not ("0" <= ch <= "9"):
            last_non_digit = index
    if last_non_digit >= 0:
        text = text[last_non_digit + 1 :]
    return text[-6:] if len(text) > 6 else text


def build_ask(
    candidate: Candidate,
    phase: Phase,
    now: datetime,
    used: dict[str, int] | None = None,
) -> Ask:
    """used 为批内维度计数，None 表示单条生成（直接走状态首选维度）。"""
    template = (
        _pick_template_for(candidate, phase, used)
        if used is not None
        else _rand_tpl(eligible_dims(candidate, phase)[0])
    )

    code = digits_of(candidate.code)
    display = template.display.replace("{name}", candidate.name).replace("{code}", code)
    query = template.ask.replace("{name}", candidate.name).replace("{code}", code)

    # ID 格式：qa_{6位码}_{UnixMilli}
    return Ask(
        id=f"qa_{code}_{int(now.timestamp() * 1000)}",
        display=display,
        query=query,
        stock_code=code,
        stock_name=candidate.name,
        tag=template.tag,
    )
