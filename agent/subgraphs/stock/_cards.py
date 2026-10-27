"""卡片事件发射：从工具结果折出候选卡、按问题意图排序、只发一张卡。

职责边界：只管"把 tool_results 折成 payload 并发出去"；不管 synthesize 的文本生成，
不管 fallback 兜底文案。synthesize 节点调用 _emit_structured(state, tool_results)。
"""
from __future__ import annotations

import logging
import re

from subgraphs import emit_card, emit_table
from subgraphs.cards import validate_card, validate_table
from subgraphs.stock._state import DATA_TOOLS

logger = logging.getLogger("stocka.agent")

# ==================== 事件类型规则（股票资讯时间轴用） ====================

# 顺序敏感：越具体的关键词排越前（「减持」与「增持」互不重叠，「诉讼」在「处罚」之前）
_EVENT_TYPE_RULES = (
    ("解禁", "解禁"),
    ("减持", "增减持"),
    ("增持", "增减持"),
    ("回购", "回购"),
    ("停牌", "停复牌"),
    ("复牌", "停复牌"),
    ("分红", "分红"),
    ("派息", "分红"),
    ("诉讼", "诉讼"),
    ("仲裁", "诉讼"),
    ("违规", "诉讼"),
    ("处罚", "诉讼"),
    ("中标", "订单"),
    ("签约", "订单"),
    ("订单", "订单"),
    ("业绩", "财报"),
    ("预告", "财报"),
    ("年报", "财报"),
    ("中报", "财报"),
    ("季报", "财报"),
)

# 需要用户在时间轴上重点关注的事件类型（前端圆点标金）
_EVENT_IMPORTANT_TYPES = ("解禁", "停复牌", "诉讼")

# 事件卡摘要的最大字数：资讯正文可能很长，卡内只放一句
_EVENT_DESC_CHARS = 60
# 摘要里数字字符的占比上限：超过说明这一段是数据表而不是叙述文字
_EVENT_DESC_DIGIT_RATIO = 0.25


def _event_desc(content) -> str:
    """资讯正文 -> 事件卡的一句摘要；正文是数据表时返回空串。"""
    text = " ".join(str(content or "").split())
    if not text:
        return ""
    cut = re.split(r"[。！？；]", text, maxsplit=1)[0].strip()
    if len(cut) < 8:
        return ""
    digits = sum(1 for ch in cut if ch.isdigit())
    if digits / len(cut) > _EVENT_DESC_DIGIT_RATIO:
        return ""
    return cut[:_EVENT_DESC_CHARS]


# ==================== 卡片选择：一次回复最多一张 ====================

# K 线（蜡烛图）话题：命中即锁定「只发 K 线卡」
_KLINE_MARKERS = (
    "k线", "蜡烛", "日k", "周k", "月k", "分钟k", "分时图", "均线", "k线图",
    "走势图", "技术分析", "技术形态", "macd", "kdj", "boll",
)

# 每张卡对应的「问题关键词」：命中越多越贴合
_CARD_KEYWORDS = {
    "compare_card": ("对比", "比较", "同业", "同行", "竞品", "谁更", "哪个好",
                     "哪家", "相比", "vs"),
    "valuation_card": ("估值", "市盈率", "市净率", "市销率", "股息", "分位", "贵不贵", "便宜"),
    "fundflow_card": ("资金", "主力", "流入", "流出", "龙虎榜", "席位", "超大单", "大单", "北向"),
    "report_card": ("财报", "业绩", "营收", "营业收入", "净利润", "毛利", "现金流",
                    "报告期", "季报", "年报", "中报"),
    "research_card": ("研报", "机构", "评级", "目标价", "券商", "观点"),
    "event_card": ("新闻", "动态", "消息", "公告", "事件", "资讯", "利好", "利空"),
    "quote_card": ("行情", "价格", "股价", "现价", "什么价", "多少钱", "涨跌", "涨幅", "跌幅",
                   "开盘", "收盘", "最高", "最低", "成交额", "换手", "市值"),
    "kline_table": ("走势", "区间", "历史", "趋势", "几个交易日", "近几日", "量能"),
}

# 英文缩写单独匹配（带词边界）
_ABBR_CARDS = {
    "pe": "valuation_card", "pb": "valuation_card", "ps": "valuation_card",
    "peg": "valuation_card", "roe": "report_card", "eps": "report_card",
    "macd": "kline_table", "kdj": "kline_table", "boll": "kline_table",
    "ma5": "kline_table", "ma10": "kline_table", "ma20": "kline_table",
}
_ABBR_RE = re.compile(
    r"(?<![a-z0-9])(pe|pb|ps|peg|roe|eps|macd|kdj|boll|ma5|ma10|ma20)(?![a-z0-9])"
)

# 走 table 事件的卡（其余都走 card 事件）
_TABLE_CARDS = {"kline_table"}


def _intent_text(state: dict) -> tuple:
    raw = ((state.get("query") or "") + " " + (state.get("rewritten") or "")).lower()
    return raw, re.sub(r"\s+", "", raw)


def _is_kline_question(state: dict) -> bool:
    _, compact = _intent_text(state)
    return any(mark in compact for mark in _KLINE_MARKERS)


def _intent_score(card_type: str, compact: str, abbrs: set) -> int:
    score = sum(1 for kw in _CARD_KEYWORDS.get(card_type, ()) if kw in compact)
    score += sum(1 for a in abbrs if _ABBR_CARDS.get(a) == card_type)
    return score


def _rank_cards(state: dict, candidates: list) -> list:
    raw, compact = _intent_text(state)
    abbrs = set(_ABBR_RE.findall(raw))
    order = {card_type: i for i, (card_type, _) in enumerate(candidates)}
    return sorted(
        candidates,
        key=lambda c: (-_intent_score(c[0], compact, abbrs), order[c[0]]),
    )


# ==================== payload 构造 ====================

def _lhb_payload(tool_results: list):
    for item in tool_results:
        if item.get("tool") != "get_top_list":
            continue
        result = item.get("result") or {}
        if result.get("ok") and isinstance(result.get("data"), dict) and result["data"].get("rows"):
            return result["data"]
    return None


def _quote_payload(tool_results: list):
    for item in tool_results:
        if item.get("tool") != "get_quote":
            continue
        result = item.get("result") or {}
        if result.get("ok") and isinstance(result.get("data"), dict) and result["data"]:
            return result["data"]
    return None


def _event_type(title: str) -> str:
    for kw, label in _EVENT_TYPE_RULES:
        if kw in title:
            return label
    return "资讯"


def _event_card_payload(news_rows: list):
    events = []
    for row in news_rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        when = str(row.get("datetime") or "")
        desc = _event_desc(row.get("content"))
        etype = _event_type(title)
        events.append({
            "type": etype,
            "date": when[:10],
            "title": title,
            "desc": desc[:_EVENT_DESC_CHARS],
            "important": etype in _EVENT_IMPORTANT_TYPES,
            "source_url": row.get("url") or "",
            "source_name": row.get("channels") or row.get("src") or "",
        })
    if not events:
        return None
    return {
        "events": events,
        "range": f"当日 · {len(events)} 条",
        "note": "事件来源：个股资讯（当日）",
    }


def _kline_rows(data: list) -> list:
    rows = [r for r in _rows_of(data) if r.get("trade_date")]
    rows.sort(key=lambda r: str(r.get("trade_date", "")))
    return [{
        "date": r.get("trade_date"),
        "open": r.get("open"),
        "high": r.get("high"),
        "low": r.get("low"),
        "close": r.get("close"),
        "volume": r.get("vol"),
    } for r in rows]


def _rows_of(data):
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return [data] if isinstance(data, dict) else []


def _split_net_wan(net):
    if net is None:
        return None, None
    w = round(net / 1e4, 2)
    return (w, 0.0) if w >= 0 else (0.0, -w)


def _fundflow_card_payload(row: dict, extra: dict | None = None) -> dict:
    main = row.get("main_net")
    payload = {
        "ts_code": row.get("ts_code"),
        "trade_date": row.get("trade_date"),
        "net_mf_amount": round(main / 1e4, 2) if main is not None else None,
    }
    for tier, src in (("elg", "super_net"), ("lg", "large_net"),
                      ("md", "mid_net"), ("sm", "small_net")):
        buy, sell = _split_net_wan(row.get(src))
        payload[f"buy_{tier}_amount"] = buy
        payload[f"sell_{tier}_amount"] = sell
    payload.update(extra or {})
    return payload


# ==================== 候选收集 + 发射 ====================

def _card_candidates(tool_results: list, state: dict | None = None) -> list:
    """把本轮工具结果折成候选卡列表 [(card_type, payload)]，保持工具调用顺序。"""
    lhb = _lhb_payload(tool_results)
    quote = _quote_payload(tool_results)
    out = []
    has_moneyflow = False
    indicator_card = None
    for item in tool_results:
        name = item.get("tool")
        result = item.get("result") or {}
        if not result.get("ok"):
            continue
        data = result.get("data")
        if not data:
            continue
        if name == "get_quote":
            out.append(("quote_card", data))
        elif name == "get_valuation":
            rows = _rows_of(data)
            if rows:
                out.append(("valuation_card", rows[0]))
        elif name == "get_money_flow":
            rows = _rows_of(data)
            has_moneyflow = bool(rows)
            if rows:
                extra = {"lhb": lhb["rows"], "lhb_date": lhb["trade_date"]} if lhb else {}
                out.append(("fundflow_card", _fundflow_card_payload(rows[0], extra)))
        elif name == "get_history_daily":
            rows = _kline_rows(data)
            if rows:
                payload = {"rows": rows}
                if quote:
                    payload["code"] = quote.get("code")
                    payload["name"] = quote.get("name")
                else:
                    symbol = ((state or {}).get("entities") or {}).get("symbol") or ""
                    if symbol:
                        payload["name"] = symbol
                out.append(("kline_table", payload))
        elif name == "get_financial_report":
            out.append(("report_card", data))
        elif name == "get_financial_indicator":
            rows = _rows_of(data)
            if rows:
                indicator_card = ("report_card", {"indicator": rows[0]})
        elif name == "get_peer_compare":
            out.append(("compare_card", data))
        elif name == "get_research_reports":
            rows = _rows_of(data.get("rows") if isinstance(data, dict) else data)
            if rows:
                out.append(("research_card", rows[0]))
        elif name == "get_stock_news":
            payload = _event_card_payload(
                _rows_of(data.get("rows") if isinstance(data, dict) else data)
            )
            if payload:
                out.append(("event_card", payload))
    if lhb and not has_moneyflow:
        out.append(("fundflow_card", {"lhb": lhb["rows"], "lhb_date": lhb["trade_date"]}))
    if indicator_card and not any(c[0] == "report_card" for c in out):
        out.append(indicator_card)
    return out


def _emit_structured(state: dict, tool_results: list) -> None:
    """按「用户问的是什么」发一张卡（不进入 token 流）。"""
    candidates = _card_candidates(tool_results, state)
    if not candidates:
        return
    ranked = _rank_cards(state, candidates)
    if _is_kline_question(state):
        for card_type, payload in ranked:
            if card_type == "kline_table" and validate_table(card_type, payload):
                emit_table(card_type, payload)
                return
        logger.info("kline question without kline table, suppress cards: %s",
                    [c[0] for c in candidates])
        return
    for card_type, payload in ranked:
        if card_type in _TABLE_CARDS:
            if validate_table(card_type, payload):
                emit_table(card_type, payload)
                return
        elif validate_card(card_type, payload):
            emit_card(card_type, payload)
            return
        logger.warning("card %s payload failed validation, try next: %s",
                       card_type, str(payload)[:200])


__all__ = ["_emit_structured", "DATA_TOOLS"]
