"""请求级上下文（contextvars）。

Go 版把 request_id 挂在 gin.Context 上；Python 侧没有每请求可变对象，
用 contextvar 承载，日志 filter 与异常处理都能直接取到，不必层层传参。
"""

from __future__ import annotations

from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def current_request_id() -> str:
    return request_id_var.get()


def set_request_id(value: str) -> None:
    request_id_var.set(value)
