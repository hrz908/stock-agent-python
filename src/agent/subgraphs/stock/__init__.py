"""stock 子图：有界 ReAct 循环，进程内工具直连取数。

拆分后模块：
- _state.py       进程级单例（工具集、绑定 LLM、DATA_TOOLS）
- _cards.py       卡片事件发射（候选收集、排序、payload 构造、_emit_structured）
- _fallback.py    兜底文案（_no_data_fallback, _failure_reason, DATA_TOOLS 判定）
- _toolcall.py    tool_call / tool_execute / synthesize 三节点 + 路由 + build_stock_subgraph
- __init__.py     本文件：对外暴露稳定 API

旧 import from subgraphs.stock import build_stock_subgraph / warm_tools 一字不变。
"""
from __future__ import annotations

from agent.subgraphs.stock._state import warm_tools
from agent.subgraphs.stock._toolcall import build_stock_subgraph

__all__ = ["build_stock_subgraph", "warm_tools"]
