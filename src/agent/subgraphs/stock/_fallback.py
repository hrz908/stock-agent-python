"""兜底文案 / 降级判定：取数失败时给确定性回答，不许模型自由发挥。

synthesize 节点 import _no_data_fallback / _failure_reason / _OVERFLOW_REPLY，
tool_call / route_after_call / route_after_execute 用到 _got_data / _timed_out 等。
"""
from __future__ import annotations

import re

from agent.core.keywords import is_concept_question
from agent.subgraphs.stock._state import DATA_TOOLS

# 命中这些词说明用户就是要数据；没调到任何数据工具时不许模型自由发挥
_DATA_KEYWORDS = (
    "k线", "K线", "行情", "价格", "涨跌", "走势", "收盘", "报价", "涨幅",
    "估值", "市盈率", "市净率", "市销率", "股息", "市值",
    "财报", "业绩", "营收", "净利润", "毛利", "ROE", "EPS",
    "资金", "主力", "龙虎榜", "流入", "流出",
    "同业", "同行", "竞品", "对比", "新闻", "动态", "消息",
)

# 英文缩写单独匹配（改写步骤常把「估值水平」缩成 PE 和 PB）
_DATA_ABBR_RE = re.compile(r"(?<![A-Za-z])(PE|PB|PS|ROE|EPS|TTM)(?![A-Za-z])")

# 客观无数据的失败文案特征：未上榜 / 当日暂无 / 没有相关记录
_GRACEFUL_EMPTY_RE = re.compile(r"未上榜|暂无|当日无|没有相关|无记录|无数据")

# 上下文超长时的最终兜底
OVERFLOW_REPLY = (
    "抱歉，这次的数据量超出了模型能一次消化的上限，我没法给出完整回答。"
    "可以缩小范围再问一次，例如「贵州茅台最近 10 天日K」「宁德时代最新一期财报」。"
)


def _got_data(state: dict) -> bool:
    """本轮是否已经取到取数类工具的数据。"""
    for item in (state.get("tool_results") or []):
        if item.get("tool") in DATA_TOOLS and (item.get("result") or {}).get("ok"):
            return True
    return False


def _asked_for_data(state: dict) -> bool:
    """用户这一轮是不是在要数据（原始问题与改写后的问题都算）。"""
    text = ((state.get("query") or "") + " " + (state.get("rewritten") or "")).upper()
    if any(k.upper() in text for k in _DATA_KEYWORDS):
        return True
    return bool(_DATA_ABBR_RE.search(text))


def _needs_data_fetch(state: dict) -> bool:
    """这一轮是否必须去取真实数据（用户要数据 + 不是概念问题）。"""
    if _is_concept_question(state):
        return False
    return _asked_for_data(state)


def _is_concept_question(state: dict) -> bool:
    text = (state.get("query") or "") + " " + (state.get("rewritten") or "")
    return is_concept_question(text)


def _all_failures_graceful_empty(tool_results: list) -> bool:
    """本轮所有取数类失败是否都是「客观无数据」而非「取数失败」。"""
    fails = [item for item in tool_results
             if item.get("tool") in DATA_TOOLS and not (item.get("result") or {}).get("ok")]
    if not fails:
        return False
    return all(
        _GRACEFUL_EMPTY_RE.search(str((item.get("result") or {}).get("error") or ""))
        for item in fails
    )


def _timed_out(tool_results: list) -> bool:
    """本轮是否有取数工具因轮级预算被截断。"""
    return any(
        item.get("tool") in DATA_TOOLS
        and str((item.get("result") or {}).get("error") or "") == "timeout"
        for item in tool_results
    )


def _failure_reason(tool_results: list) -> str:
    """从失败的工具结果里提炼一句准确的失败原因。"""
    for item in tool_results:
        res = item.get("result") or {}
        if res.get("ok"):
            continue
        err = str(res.get("error") or "")
        if not err:
            continue
        low = err.lower()
        if "频率" in err or "频繁" in err or "rate" in low:
            return "数据源访问频率受限，请稍后再试"
        if "权限" in err or "没有接口" in err or "forbidden" in low or "403" in low:
            return "数据源暂无法提供该数据（接口权限或数据范围受限）"
        if "retry" in low or "connection" in low or "连接" in err or "登录" in err:
            return "数据源连接失败，请稍后再试"
        if "timeout" in low or "timed out" in low:
            return "数据接口响应超时"
        return err[:80]
    return ""


def _subject(state: dict) -> str:
    """兜底文案里回显问题主体（改写后优先）。

    不写死示例标的：多轮追问「最近成交量有放大吗」在改写后已经带上「比亚迪」，
    回显它才能让用户确认问的是哪只票；写死「000568」会把上下文彻底丢掉。
    """
    text = re.sub(r"\s+", " ", (state.get("rewritten") or state.get("query") or "")).strip()
    if not text:
        return ""
    return text[:30] + ("…" if len(text) > 30 else "")


def _no_data_fallback(state: dict, tool_results: list) -> str:
    """本轮没有取到任何行情数据、且用户就是要数据时，返回固定兜底文案。

    宁可说「没取到」，也不给过期/编造的数字。
    """
    # 有任一取数类工具成功 -> 数据到手，交给模型作答
    if any(
        item.get("tool") in DATA_TOOLS and (item.get("result") or {}).get("ok")
        for item in tool_results
    ):
        return ""
    # 模型调过取数类工具却全部失败 -> 用户问的就是数据，必然要兜底
    called_data_tool = any(item.get("tool") in DATA_TOOLS for item in tool_results)
    if called_data_tool:
        reason = _failure_reason(tool_results)
        if _all_failures_graceful_empty(tool_results):
            return (f"查询完成：{reason or '暂无相关数据'}。"
                    "这是数据源当前的客观情况，并非系统故障；可以换个标的或时间范围再试。")
        head = f"关于「{_subject(state)}」，这一轮没有取到所需的数据，我无法给出你要的数字。"
        if reason:
            head += reason + "。"
        return head + "请稍后重试；也可以带上股票代码再问，例如「000568 最近15个交易日的日K」。"
    # 没调取数工具：可能是知识型问题，兜底会误伤能答好的回答
    if _is_concept_question(state):
        return ""
    if _asked_for_data(state):
        return (f"关于「{_subject(state)}」，这一轮没有取到所需的数据，我无法给出你要的数字。"
                "请稍后重试；也可以带上股票代码再问，例如「000568 最近15个交易日的日K」。")
    return ""


__all__ = [
    "OVERFLOW_REPLY", "_no_data_fallback", "_failure_reason",
    "_got_data", "_asked_for_data", "_needs_data_fetch",
    "_is_concept_question", "_all_failures_graceful_empty", "_timed_out",
]
