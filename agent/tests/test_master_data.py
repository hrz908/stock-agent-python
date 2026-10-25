#!/usr/bin/env python
"""A股主数据只读链路测试（不连库、不联网）。

覆盖迁移 008 接进 agent 之后的四件事：
  1. repo.stock.industry_peers：两段查询、空结果口径（股票不存在 / 没有行业）、入参清洗；
  2. get_industry_peers：入参收敛（非法代码、top_n 上限）与降级（库不可用不抛异常）；
  3. 接进 ReAct 的三处：工具注册、tool_start 标签、文本形态工具调用还原；
  4. get_peer_compare：peers 代码归一化、去重、排除本股。

用法：python -X utf8 tests/test_master_data.py
（Windows 控制台不加 -X utf8 会因编码在打印中文时抛异常）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import repo.stock as repo_stock  # noqa: E402
import tools.compare as compare  # noqa: E402
from repo.stock import industry_peers  # noqa: E402
from subgraphs.stock import _DATA_TOOLS, _TOOL_LABELS, _infer_tool_by_args  # noqa: E402
from tools import get_tools  # noqa: E402
from tools.compare import _auto_peers, _split_peers, get_peer_compare  # noqa: E402
from tools.peers import get_industry_peers  # noqa: E402

_FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _FAILED.append(name)


def patch_fetch(rows_by_call):
    """把 repo.stock.fetch_all 换成按调用顺序返回预设行的假实现，并记录入参。"""
    calls = []

    async def fake(sql, params=None):
        calls.append(params or {})
        return rows_by_call[min(len(calls) - 1, len(rows_by_call) - 1)]

    repo_stock.fetch_all = fake
    return calls


# ===== 1. 仓储口径 =====
def test_industry_peers():
    print("\n[1] industry_peers：两段查询与结果映射")
    calls = patch_fetch([
        [{"code": "600519", "name": "贵州茅台", "market": "SH", "industry_l1": "食品饮料",
          "industry_l2": "饮料", "industry_l3": "白酒"}],
        [{"code": "000858", "name": "五粮液", "market": "SZ"}],
    ])
    data = asyncio.run(industry_peers("600519", limit=3))

    check("返回本股信息", data["name"] == "贵州茅台" and data["market"] == "SH")
    check("返回一级行业名", data["industry"] == "食品饮料")
    check("返回完整行业路径", data["industry_detail"] == "食品饮料-饮料-白酒",
          data["industry_detail"])
    check("peers 只保留 code/name/market",
          data["peers"] == [{"code": "000858", "name": "五粮液", "market": "SZ"}],
          str(data["peers"]))
    check("行业三级与上限下推到 SQL",
          calls[1].get("l1") == "食品饮料" and calls[1].get("l2") == "饮料"
          and calls[1].get("l3") == "白酒" and calls[1].get("limit") == 3, str(calls[1]))
    check("分层排序是 ORDER BY 的第一顺位", "ORDER BY tier" in repo_stock._PEERS_SQL)
    check("行业取数东财优先", "(std = 'em') DESC" in repo_stock._INDUSTRY_LATERAL)

    print("\n[2] industry_peers：空结果口径")
    calls = patch_fetch([[]])
    data = asyncio.run(industry_peers("999999"))
    check("股票不存在时 peers 为空且不报错", data["peers"] == [] and data["code"] == "999999")
    check("只查一次（没有行业就不查 peers）", len(calls) == 1, str(calls))

    calls = patch_fetch([
        [{"code": "600519", "name": "贵州茅台", "market": "SH", "industry_l1": ""}],
    ])
    data = asyncio.run(industry_peers("600519"))
    check("没有行业时 peers 为空", data["peers"] == [] and data["industry"] == "")
    check("同样只查一次", len(calls) == 1, str(calls))


# ===== 2. 工具入参与降级 =====
def test_tool_guardrails():
    print("\n[3] get_industry_peers：入参收敛")
    calls = []

    async def fake_repo(code, limit=6, include_st=False):
        calls.append((code, limit))
        return {"code": code, "name": "贵州茅台", "market": "SH", "industry": "酿酒行业",
                "peers": [{"code": "000858", "name": "五粮液", "market": "SZ"}]}

    repo_stock.industry_peers = fake_repo

    bad = asyncio.run(get_industry_peers.ainvoke({"code": "abc"}))
    check("非法代码直接失败，不查库", bad.get("ok") is False and calls == [], str(bad))

    ok = asyncio.run(get_industry_peers.ainvoke({"code": "SH600519", "top_n": 999}))
    check("带市场前缀也能抽出 6 位代码", calls[-1][0] == "600519", str(calls))
    check("top_n 截到 20", calls[-1][1] == 20, str(calls))
    check("成功时包成 {ok, data} 信封",
          ok.get("ok") is True and "peers" in ok.get("data", {}), str(ok))

    print("\n[4] get_industry_peers：库不可用时降级")
    async def boom(*args, **kwargs):
        raise RuntimeError("connecting to postgres failed")

    repo_stock.industry_peers = boom
    res = asyncio.run(get_industry_peers.ainvoke({"code": "600519"}))
    check("异常被吞成 {ok: False}", res.get("ok") is False, str(res))
    check("错误信息可读", "postgres" in str(res.get("error", "")), str(res))


# ===== 3. 同业对比自动补候选 =====
def test_compare_auto_peers():
    print("\n[5] get_peer_compare：peers 留空时按行业补两家")

    async def fake_repo(code, limit=6, include_st=False):
        return {"peers": [{"code": "000858"}, {"code": "000568"}, {"code": "600809"}]}

    repo_stock.industry_peers = fake_repo
    codes = asyncio.run(_auto_peers("600519"))
    check("只取前两家", codes == ["000858", "000568"], str(codes))

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    repo_stock.industry_peers = boom
    check("仓储不可用时返回空（退化为只比本股）", asyncio.run(_auto_peers("600519")) == [])


# ===== 4. ReAct 接线 =====
def test_wiring():
    print("\n[6] ReAct 接线")
    check("文本形态调用能还原成新工具",
          _infer_tool_by_args({"code": "600519", "top_n": 6}) == "get_industry_peers")
    check("code + limit 仍归资讯（避免误判成同业工具）",
          _infer_tool_by_args({"code": "600519", "limit": 5}) == "get_stock_news")
    check("tool_start 有中文标签", _TOOL_LABELS.get("get_industry_peers") == "同行业公司")
    names = [t.name for t in get_tools()]
    check("已注册进工具集", "get_industry_peers" in names, str(names))
    check("同业工具不产数字，不进 _DATA_TOOLS", "get_industry_peers" not in _DATA_TOOLS)


# ===== 5. 同业对比入参：写法归一与本股去重 =====
def test_compare_inputs():
    print("\n[7] _split_peers：代码归一化、去重、排除本股")
    check("逗号 + 空格", _split_peers("000858, 600809") == ["000858", "600809"],
          str(_split_peers("000858, 600809")))
    check("点号 / 前缀写法归一成 6 位裸码",
          _split_peers("000858.SZ,SH600809") == ["000858", "600809"],
          str(_split_peers("000858.SZ,SH600809")))
    check("同股不同写法只留一只，且排除本股",
          _split_peers("600519,SH600519,600519.SH", "600519") == [],
          str(_split_peers("600519,SH600519,600519.SH", "600519")))
    check("非代码脏值丢弃", _split_peers("贵州茅台,,000858") == ["000858"],
          str(_split_peers("贵州茅台,,000858")))
    check("仍截到 2 家", _split_peers("000001,000002,000003") == ["000001", "000002"],
          str(_split_peers("000001,000002,000003")))

    print("\n[8] get_peer_compare：本股不占对比列")
    seen, auto_calls = [], []

    async def fake_fetch(c):
        seen.append(c)
        return {"code": c, "name": c}

    async def fake_auto(c):
        auto_calls.append(c)
        return ["000858"]

    orig = (compare._fetch_one, compare._auto_peers)
    compare._fetch_one, compare._auto_peers = fake_fetch, fake_auto
    try:
        res = asyncio.run(get_peer_compare.ainvoke(
            {"code": "600519", "peers": "600519,600519.SH,000858"}))
        check("本股只请求一次", seen == ["600519", "000858"], str(seen))
        check("显式 peers 时 source=explicit",
              res["data"]["peers_source"] == "explicit", str(res))
        check("ts_code 用归一后的代码", res["data"]["ts_code"] == "600519.SH", str(res))

        seen.clear()
        res = asyncio.run(get_peer_compare.ainvoke({"code": "600519", "peers": "SH600519"}))
        check("peers 全是本股时退回按行业补", auto_calls == ["600519"], str(auto_calls))
        check("退回补候选时 source=industry",
              res["data"]["peers_source"] == "industry" and seen == ["600519", "000858"],
              f"{seen} / {res['data']['peers_source']}")

        seen.clear()
        res = asyncio.run(get_peer_compare.ainvoke({"code": "abc"}))
        check("本股代码非法直接失败，不发行情请求",
              res.get("ok") is False and seen == [], str(res))
    finally:
        compare._fetch_one, compare._auto_peers = orig


# ===== 6. 仓储入参清洗 =====
def test_repo_input_guard():
    print("\n[9] industry_peers：入参清洗")
    calls = patch_fetch([[]])
    data = asyncio.run(industry_peers("600519.SH"))
    check("非 6 位裸码不查库，直接给空结构",
          data["peers"] == [] and data["industry"] == "" and calls == [], str(calls))

    rows = [
        [{"code": "600519", "name": "贵州茅台", "market": "SH", "industry_l1": "食品饮料"}],
        [],
    ]
    calls = patch_fetch(rows)
    asyncio.run(industry_peers(" 600519 "))
    check("首尾空白清掉再查库", calls[0].get("code") == "600519", str(calls[:1]))

    calls = patch_fetch(rows)
    asyncio.run(industry_peers("600519", limit=0))
    check("limit=0 收敛为 1（不会 LIMIT 0）", calls[1].get("limit") == 1, str(calls[1:2]))

    calls = patch_fetch(rows)
    asyncio.run(industry_peers("600519", limit=None))
    check("limit=None 回落默认 6", calls[1].get("limit") == 6, str(calls[1:2]))

    calls = patch_fetch(rows)
    asyncio.run(industry_peers("600519", limit="abc"))
    check("limit 非数字回落默认 6", calls[1].get("limit") == 6, str(calls[1:2]))

    calls = patch_fetch(rows)
    asyncio.run(industry_peers("600519", include_st="false"))
    check("include_st 强制成 bool（psycopg 不接受字符串）",
          calls[1].get("include_st") is False, str(calls[1:2]))


def main():
    print("=" * 62)
    print("A股主数据只读链路测试")
    print("=" * 62)
    test_industry_peers()
    test_tool_guardrails()
    test_compare_auto_peers()
    test_wiring()
    test_compare_inputs()
    test_repo_input_guard()
    print("\n" + "=" * 62)
    if _FAILED:
        print(f"FAILED: {len(_FAILED)} 项 -> {_FAILED}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
