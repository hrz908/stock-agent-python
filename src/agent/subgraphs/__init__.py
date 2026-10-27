"""子图公共能力：结构化事件（card/table）发射。

结构化数据（行情卡片、K线表格）必须走独立的 custom 事件，绝不混入 token 流；
用 langgraph 的 stream writer 发射，writer 不可用时静默降级（不阻断生成）。
"""
from typing import Any

try:
    from langgraph.config import get_stream_writer
except Exception:  # pragma: no cover - 兼容不同 langgraph 版本
    get_stream_writer = None  # type: ignore


def _writer():
    if get_stream_writer is None:
        return None
    try:
        return get_stream_writer()
    except Exception:  # noqa: BLE001 - 不在图运行上下文里时拿不到 writer
        return None


def emit(event: str, data: Any) -> None:
    """发射一个 custom 事件，形如 {"event": ..., "data": ...}。"""
    w = _writer()
    if w is not None:
        w({"event": event, "data": data})


def emit_card(card_type: str, payload: Any) -> None:
    emit("card", {"type": card_type, "payload": payload})


def emit_table(table_type: str, payload: Any) -> None:
    emit("table", {"type": table_type, "payload": payload})
