"""真数据跑卡片折叠：一次一张卡、同类卡不重复、行级去重（不进 CI，需要外网）。

为什么单独一个文件：其余卡片测试全用手写 payload，字段口径漂了也发现不了。
这里直接打真实数据源（akshare/新浪/baostock + 本地网关），把结果原样喂给
`_card_candidates` / `_emit_structured`，验证三件事：

  1. 每个候选卡的载荷都能过 `validate_card` / `validate_table`（金额单位、必填字段）；
  2. 每个问句只发一张卡，且卡片类型与问题意图对得上；
  3. 同一批结果重复喂一遍，候选与卡片都不翻倍（2026-10-27 去重改动的回归）。

用法：
    PYTHONPATH=src python tests/integration/test_card_realdata.py

前置：能连外网。行情类工具（get_quote / get_peer_compare）走本地网关
127.0.0.1:8080（GO_SERVER_URL），没起服务时这两项会降级为失败——脚本只要求
「一半以上工具取到数据」，并把每个工具的耗时/大小打出来方便比对。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

# Windows 控制台默认 cp1252，直接 print 中文会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001, S110 - 老终端不支持时忽略
    pass

from agent.subgraphs.cards import validate_card, validate_table  # noqa: E402
from agent.subgraphs.stock import _cards as C  # noqa: E402
from agent.tools import get_tools  # noqa: E402

FAILED = []

CODE = "600519"  # 贵州茅台：八类卡的数据都有
CALLS = [
    ("get_quote", {"code": CODE}),
    ("get_valuation", {"code": CODE}),
    ("get_money_flow", {"code": CODE}),
    ("get_history_daily", {"code": CODE, "days": 15}),
    ("get_stock_news", {"code": CODE, "limit": 20}),
    ("get_top_list", {"code": CODE}),
    ("get_financial_report", {"code": CODE}),
    ("get_peer_compare", {"code": CODE, "peers": "000858,600809"}),
    ("get_research_reports", {"code": CODE, "limit": 3}),
]
# 问句 -> 期望卡片；期望只是参考（打分按关键词命中数，数据缺失时合法偏移不判失败）
QUERIES = [
    ("茅台现在什么价", "quote_card"),
    ("贵州茅台的估值贵不贵", "valuation_card"),
    ("茅台今天资金流向怎么样", "fundflow_card"),
    ("茅台最近财务表现如何", "report_card"),
    ("茅台最近有什么新闻", "event_card"),
    ("茅台最近的机构研报观点", "research_card"),
    ("茅台和五粮液对比一下", "compare_card"),
    ("茅台最近日K走势", "kline_table"),
]
_CARD_TYPES = {
    "quote_card", "valuation_card", "fundflow_card", "report_card",
    "compare_card", "research_card", "event_card",
}


def check(name: str, cond: bool, detail: object = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILED.append(name)


async def _fetch_all() -> list[dict]:
    tools = {t.name: t for t in get_tools()}

    async def one(name: str, args: dict):
        t0 = time.monotonic()
        try:
            res = await asyncio.wait_for(tools[name].ainvoke(args), timeout=45)
        except Exception as e:  # noqa: BLE001 - 取数失败按降级结果处理
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return name, args, res, int((time.monotonic() - t0) * 1000)

    outcomes = await asyncio.gather(*[one(n, a) for n, a in CALLS])
    out = []
    for name, args, res, ms in outcomes:
        data = res.get("data")
        size = len(json.dumps(data, ensure_ascii=False, default=str)) if data is not None else 0
        print(f"  {'OK ' if res.get('ok') else 'ERR'} {name:22s} {ms:5d}ms {size:6d}B"
              + ("" if res.get("ok") else f"  {(res.get('error') or '')[:80]}"))
        out.append({"tool": name, "args": args, "result": res})
    return out


def _emit(query: str, results: list) -> list[tuple[str, dict]]:
    """跑一次发卡，返回 [(type, payload)]；emit 被替换成收集器。"""
    got: list[tuple[str, dict]] = []
    orig_card, orig_table = C.emit_card, C.emit_table
    C.emit_card = lambda t, p: got.append((t, p))
    C.emit_table = lambda t, p: got.append((t, p))
    try:
        C._emit_structured({"query": query, "rewritten": ""}, results)
    finally:
        C.emit_card, C.emit_table = orig_card, orig_table
    return got


def main() -> int:
    print("=" * 62)
    print(f"[1] 真数据取数（{CODE}）")
    results = asyncio.run(_fetch_all())
    ok_tools = [r for r in results if r["result"].get("ok")]
    check("至少一半工具取到数据", len(ok_tools) * 2 >= len(results),
          f"{len(ok_tools)}/{len(results)}")

    print("\n[2] 候选卡：同类不重复、载荷全部合规")
    cands = C._card_candidates(results, {"entities": {"symbol": "贵州茅台"}})
    types = [t for t, _ in cands]
    print("  候选：" + ", ".join(types))
    check("候选里同类卡不重复", len(types) == len(set(types)), str(types))
    for card_type, payload in cands:
        ok = (validate_table(card_type, payload) if card_type not in _CARD_TYPES
              else validate_card(card_type, payload))
        check(f"候选载荷过校验：{card_type}", ok, str(payload)[:160])

    print("\n[3] 每个问句只出一张卡")
    for query, expect in QUERIES:
        got = _emit(query, results)
        card_type = got[0][0] if got else ""
        valid = bool(got) and (
            validate_table(card_type, got[0][1]) if card_type not in _CARD_TYPES
            else validate_card(card_type, got[0][1])
        )
        check(f"一张合法卡：{query}", len(got) == 1 and valid, str([t for t, _ in got]))
        if expect and got and card_type != expect:
            # 数据缺失（网关没起 / 龙虎榜未上榜）时会正常偏移到下一张，不算失败
            print(f"        参考：期望 {expect}，实际 {card_type}")

    print("\n[4] 同一批结果喂两遍：候选与卡片都不翻倍")
    doubled = results + [{**r, "result": json.loads(json.dumps(r["result"], default=str))}
                         for r in results]
    cands2 = C._card_candidates(doubled, {"entities": {"symbol": "贵州茅台"}})
    check("候选数量不变", len(cands2) == len(cands), f"{len(cands)} -> {len(cands2)}")
    for query, _ in QUERIES:
        check(f"重复数据仍只发一张卡：{query}", len(_emit(query, doubled)) == 1)

    print("\n[5] 卡片内行去重")
    news = next((r["result"].get("data") for r in results
                 if r["tool"] == "get_stock_news" and r["result"].get("ok")), None)
    if news:
        rows = news if isinstance(news, list) else (news.get("rows") or [])
        payload = C._event_card_payload(rows) or {}
        titles = [e["title"] for e in payload.get("events", [])]
        check("事件卡有内容且标题不重复", bool(titles) and len(titles) == len(set(titles)),
              str(titles))
    else:
        print("  SKIP  资讯数据不可用")

    kline = next((r["result"].get("data") for r in results
                  if r["tool"] == "get_history_daily" and r["result"].get("ok")), None)
    if kline:
        rows = C._kline_rows(kline)
        dates = [r["date"] for r in rows]
        check("K 线交易日不重复且升序", len(dates) == len(set(dates)) and dates == sorted(dates),
              str(dates))
        check("K 线不超过 15 根", len(rows) <= 15, str(len(rows)))
    else:
        print("  SKIP  历史行情不可用")

    print("\n" + "=" * 62)
    if FAILED:
        print(f"FAILED: {len(FAILED)} 项 -> {FAILED}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - 脚本入口兜底，打印栈后非零退出
        traceback.print_exc()
        sys.exit(2)
