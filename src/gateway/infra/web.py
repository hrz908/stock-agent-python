"""请求体读取与校验小工具。

对齐 Go 版逐接口的 MaxBytesReader 语义：绑定失败与会话/登录等业务错误
要回同一个中文提示，不能把 Pydantic 的 422 细节漏给前端。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request

from gateway.infra.resp import CODE_BAD_REQUEST, ApiError


async def read_body(request: Request, max_bytes: int, error_message: str) -> bytes:
    """读取原始请求体并限制大小；超限按「格式错误」处理（同 Go 的绑定失败）。"""
    raw = await request.body()
    if max_bytes > 0 and len(raw) > max_bytes:
        raise ApiError(CODE_BAD_REQUEST, error_message)
    return raw


async def read_json(
    request: Request, max_bytes: int = 0, error_message: str = "请求体格式错误"
) -> dict[str, Any]:
    """读取 JSON 对象；非对象/解析失败/超限统一抛 400。"""
    raw = await read_body(request, max_bytes, error_message)
    if not raw:
        raise ApiError(CODE_BAD_REQUEST, error_message)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiError(CODE_BAD_REQUEST, error_message) from exc
    if not isinstance(payload, dict):
        raise ApiError(CODE_BAD_REQUEST, error_message)
    return payload


def require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def require_int(payload: dict[str, Any], key: str) -> int | None:
    """取整数；兼容 JSON number 与数字字符串，取不到返回 None。"""
    value = payload.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None
