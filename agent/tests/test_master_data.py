#!/usr/bin/env python
"""A股主数据只读链路测试（不连库、不联网）。

覆盖迁移 008 接进 agent 之后的三件事：
  1. repo.stock.industry_peers：两段查询、空结果口径（股票不存在 / 没有行业）；
  2. get_industry_peers：入参收敛（非法代码、top_n 上限）与降级（库不可用不抛异常）；
  3. 接进 ReAct 的三处：工具注册、tool_start 标签、文本形态工具调用还原。

用法：python -X utf8 tests/test_master_data.py
（Windows 控制台不加 -X utf8 会因编码在打印中文时抛异常）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 不拉起 MCP stdio 子进程：本套用例只验进程内工具与仓储
os.environ["STOCK_MCP_ENABLED"] = "0"

import repo.stock as repo_stock  # noqa: E402
from repo.stock import industry_peers  # noqa: E402
from subgraphs.stock import _DATA_TOOLS, _TOOL_LABELS, _infer_tool_by_args  # noqa: E402
from tools import get_tools  # noqa: E402
from tools.compare import _auto_peers  # noqa: E402
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


def main():
    print("=" * 62)
    print("A股主数据只读链路测试")
    print("=" * 62)
    test_industry_peers()
    test_tool_guardrails()
    test_compare_auto_peers()
    test_wiring()
    print("\n" + "=" * 62)
    if _FAILED:
        print(f"FAILED: {len(_FAILED)} 项 -> {_FAILED}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
