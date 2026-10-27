"""统一响应体 {code, message, data} 与业务错误。

与 Go 版 internal/resp 逐字段对齐：
- code=0 表示成功，其余与 HTTP 状态码同值；
- 字段顺序固定 code → message → data（dict 保序，保证与缓存字节一致）；
- 金额/价格等数值由各 handler 自行转字符串，这一层不做任何类型转换。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

CODE_OK = 0
CODE_BAD_REQUEST = 400
CODE_UNAUTHORIZED = 401
CODE_NOT_FOUND = 404
CODE_CONFLICT = 409
CODE_TOO_MANY_REQUESTS = 429
CODE_INTERNAL = 500
CODE_BAD_GATEWAY = 502

_STATUS_MAP = {
    CODE_OK: 200,
    CODE_UNAUTHORIZED: 401,
    CODE_BAD_REQUEST: 400,
    CODE_NOT_FOUND: 404,
    CODE_CONFLICT: 409,
    CODE_TOO_MANY_REQUESTS: 429,
    CODE_BAD_GATEWAY: 502,
}


def http_status(code: int) -> int:
    """业务码 → HTTP 状态码；未登记的一律 500。"""
    return _STATUS_MAP.get(code, 500)


class ApiError(Exception):
    """业务错误：由全局异常处理器统一渲染成信封。

    handler 里 raise 即可，等价于 Go 的 resp.Fail(c, code, msg) + return。
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def body(code: int, message: str, data: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}


def json_bytes(code: int, message: str, data: Any) -> bytes:
    """序列化信封为字节。用于「同一份响应既写缓存又返回」的接口（股票搜索等），
    保证缓存内容与真实响应逐字节一致，避免两边结构漂移。"""
    return json.dumps(
        body(code, message, data), ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")


def ok_response(data: Any = None) -> JSONResponse:
    return JSONResponse(content=body(CODE_OK, "ok", data), status_code=200)


def fail_response(code: int, message: str) -> JSONResponse:
    return JSONResponse(content=body(code, message, None), status_code=http_status(code))


def cached_json_response(payload: bytes) -> Response:
    """直接回写已序列化好的信封字节（带 X-Cache 等头的场景）。"""
    return Response(content=payload, media_type="application/json; charset=utf-8")


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return fail_response(exc.code, exc.message)
