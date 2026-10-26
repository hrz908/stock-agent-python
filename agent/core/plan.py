"""确定性工具规划：常见取数问句直接产出 tool_calls，跳过 tool_call 的 LLM 决策。

线上实测（2026-10-26，agent_runs 审计）：stock_agent 的首 token 里
`tool_call_ms` 占 1.4~2.8s，是等待时间最大的一块；而「某票现在什么价 / 资金流向 /
K线 / 估值 / 新闻 / 研报」这类问句的工具选择其实是确定的：命中关键词 → 一两个工具
+ 一个代码参数。这里按关键词直接生成 tool_calls，把这段 LLM 往返省掉。

只在「能确定」时接管，其余一律返回 None 回退原来的 LLM 决策：
- 问题命中已知工具族，且不是对比/同业这类要两个标的的（那需要模型挑同行）；
- 标的是唯一的（6 位代码，或名称表命中恰好一家）；
- 概念题（解释/区别/是什么）不接管——那是知识库的活；
- 最多两个工具：并行取数的耗时由最慢的那个决定，再多只会把首屏拖后。
"""
import re

from core.keywords import hit_data_keyword, is_concept_question
from rag.entity import entity_codes

# 工具族 -> 触发词。顺序即同分时的优先级：行情最常问，排在最前。
_RULES = (
    ("get_quote", (
        "什么价", "现价", "股价", "价格", "行情", "涨跌", "涨幅", "跌幅",
        "收盘", "开盘", "最高", "最低", "成交额", "成交量", "换手", "涨停", "跌停", "报价",
    )),
    ("get_money_flow", (
        "资金", "主力", "净流入", "净流出", "流入", "流出", "龙虎榜", "席位",
    )),
    ("get_valuation", (
        "估值", "市盈率", "市净率", "市销率", "股息", "贵不贵", "便宜", "peg",
    )),
    ("get_history_daily", (
        "k线", "日k", "周k", "月k", "蜡烛", "均线", "走势", "技术形态", "macd", "kdj", "boll",
    )),
    ("get_stock_news", ("新闻", "消息", "公告", "动态", "资讯")),
    ("get_research_reports", ("研报", "评级", "目标价", "机构观点", "盈利预测")),
    ("get_financial_report", ("财报", "业绩", "营收", "净利润", "毛利", "现金流")),
)

# 需要两个以上标的/需要模型挑同行的问题，交还给 LLM（它会先搜候选再比）
_DEFER_WORDS = ("对比", "比较", "同业", "同行", "竞品", "哪个好", "谁更", "vs")

# 英文指标整词匹配：直接子串会把 "pe" 撞进 "peers"
_ABBR_RE = re.compile(r"(?<![a-zA-Z])(pe|pb|ps|peg)(?![a-zA-Z])", re.IGNORECASE)

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# K 线天数：「最近5个交易日」「近 10 日」「3天」；工具上限 15
_DAYS_RE = re.compile(r"(?:最近|近|过去)?\s*(\d{1,2})\s*(?:个)?(?:交易日|日|天)")

_MIN_DAYS = 1
_MAX_DAYS = 15
_DEFAULT_DAYS = 5

# 一条问句最多并行几个工具（最慢的那个决定首屏）
_MAX_PLAN_TOOLS = 2


def _clip_days(raw: str) -> int:
    m = _DAYS_RE.search(raw or "")
    if not m:
        return _DEFAULT_DAYS
    try:
        n = int(m.group(1))
    except ValueError:
        return _DEFAULT_DAYS
    return max(_MIN_DAYS, min(_MAX_DAYS, n))


def _matched_families(text: str) -> list[tuple[str, int]]:
    """命中的工具族及命中词数，按命中数降序、同分按 _RULES 顺序。"""
    low = (text or "").lower()
    hits = []
    for order, (name, words) in enumerate(_RULES):
        n = sum(1 for w in words if w in low)
        if name == "get_valuation" and _ABBR_RE.search(low):
            n += 1
        if n:
            hits.append((order, name, n))
    hits.sort(key=lambda x: (-x[2], x[0]))
    return [(name, n) for _, name, n in hits]


def _args_for(tool: str, code: str, query: str) -> dict:
    if tool == "get_history_daily":
        return {"code": code, "days": _clip_days(query)}
    return {"code": code}


async def _resolve_code(query: str) -> str:
    """问题里的标的代码；唯一确定不了就返回空串。"""
    codes = _CODE_RE.findall(query or "")
    if codes:
        return codes[0]
    ents = await entity_codes(query)
    if len(ents) == 1:
        return next(iter(ents))
    return ""


def _symbol_from_results(tool_results: list) -> str:
    """上一轮 symbol_search 已经拿到代码时直接用它（名称问句的第二跳）。"""
    for item in tool_results:
        if item.get("tool") != "symbol_search":
            continue
        res = item.get("result") or {}
        if not res.get("ok"):
            continue
        for row in (res.get("data") or [])[:1]:
            if isinstance(row, dict) and row.get("code"):
                return str(row["code"])
    return ""


async def fast_tool_calls(query: str, tool_results: list | None = None) -> list[dict] | None:
    """命中确定性规则时返回 tool_calls 列表，否则 None（交回 LLM）。

    tool_results 只用来复用上一轮 symbol_search 的结果；本轮已经取到数据时
    调用方不会走到这里（见 subgraphs/stock.py 的 tool_call）。
    """
    text = (query or "").strip()
    if not text:
        return None
    if any(w in text for w in _DEFER_WORDS):
        return None
    if is_concept_question(text) or not hit_data_keyword(text):
        return None

    families = _matched_families(text)[:_MAX_PLAN_TOOLS]
    if not families:
        return None

    code = _symbol_from_results(tool_results or []) or await _resolve_code(text)
    if not code:
        return None

    out = []
    for i, (tool, _n) in enumerate(families):
        out.append({
            "name": tool,
            "args": _args_for(tool, code, text),
            "id": f"plan_{tool}_{i}",
            "type": "tool_call",
        })
    return out



