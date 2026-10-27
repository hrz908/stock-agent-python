"""网关全局中间件（纯 ASGI 实现）。

为什么不用 BaseHTTPMiddleware：它会用内存流包装响应，SSE 帧会被攒批下发，
实时性直接失效。这里手写 ASGI，只拦截 http.response.start 注入响应头，
其余消息原样透传，流式一行都不缓冲。

执行顺序对齐 Go 版 router.go：
  Recovery → RequestID → CORS → IPRateLimit → AccessLog（→ /api/v1 的 BodyLimit）
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server.config import Config
from server.context import request_id_var
from server.middleware.clientip import resolve_client_ip
from server.middleware.rate_limit import SlidingWindowLimiter
from server.resp import (
    CODE_BAD_REQUEST,
    CODE_INTERNAL,
    CODE_TOO_MANY_REQUESTS,
    ApiError,
    body,
    http_status,
)

logger = logging.getLogger("stocka.server.http")

REQUEST_ID_HEADER = b"x-request-id"
REQUEST_ID_MAX_LEN = 64
# 业务接口请求体上限：头像 2MB、对话 64KB、登录 4KB 各自更严，
# 4MB 只是兜住 ShouldBindJSON 把超大 body 读进内存的兜底值
API_BODY_LIMIT = 4 << 20
# 头像多出的 1KB 余量给 multipart 头与边界：客户端可以伪造 file.Size，
# 真正的限制必须落在「写入字节数」上
AVATAR_BODY_LIMIT = (2 << 20) + 1024
LOGIN_BODY_LIMIT = 4 << 10
STREAM_BODY_LIMIT = 64 << 10

# (路径后缀, 上限, 超限提示)：逐接口收紧，提示语与 Go 版各 handler 保持一致
_BODY_RULES: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("/user/avatar",), AVATAR_BODY_LIMIT, "头像不能超过 2MB"),
    (("/auth/login",), LOGIN_BODY_LIMIT, "缺少登录 code"),
    (("/agent/stream",), STREAM_BODY_LIMIT, "请求体格式错误"),
)
ALLOWED_METHODS = "GET,POST,PUT,DELETE,OPTIONS"
ALLOWED_HEADERS = "Content-Type,Authorization,X-Request-ID"
CORS_MAX_AGE = "86400"


def sanitize_request_id(raw: str) -> str:
    """只留可打印 ASCII 并限长，其余视为未传（重新生成）。

    不限制会让超长值/控制字符进日志，造成日志注入与洪水。
    """
    text = (raw or "").strip()
    if not text or len(text) > REQUEST_ID_MAX_LEN:
        return ""
    for ch in text:
        if ord(ch) < 0x20 or ord(ch) > 0x7E:
            return ""
    return text


def _parse_origins(raw: str) -> set[str]:
    out = {part.strip() for part in (raw or "").split(",") if part.strip()}
    return out or {"*"}


def _decode(headers: dict[bytes, bytes], key: bytes) -> str:
    return headers.get(key, b"").decode("latin-1", "ignore")


class GatewayMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        cfg: Config,
        limiter_getter: Callable[[], SlidingWindowLimiter | None],
    ) -> None:
        self.app = app
        self.cfg = cfg
        # 用 getter 而不是实例：中间件在 create_app 时装配，limiter 要等 lifespan 建完 Redis 才有
        self._limiter_getter = limiter_getter
        self.allow_origins = _parse_origins(cfg.allow_origins)
        self.ip_per_min = cfg.ratelimit.ip_per_min

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET")
        headers = dict(scope.get("headers") or [])

        rid = sanitize_request_id(_decode(headers, REQUEST_ID_HEADER)) or uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        state = {"status": 500, "sent": False}

        origin = _decode(headers, b"origin")
        allowed_origin = (
            origin
            if origin and (origin in self.allow_origins or "*" in self.allow_origins)
            else ""
        )

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["sent"] = True
                state["status"] = message.get("status", 500)
                out = MutableHeaders(scope=message)
                out["X-Request-ID"] = rid
                if allowed_origin:
                    out["Access-Control-Allow-Origin"] = allowed_origin
                    out["Vary"] = "Origin"
                out["Access-Control-Allow-Methods"] = ALLOWED_METHODS
                out["Access-Control-Allow-Headers"] = ALLOWED_HEADERS
                out["Access-Control-Max-Age"] = CORS_MAX_AGE
            await send(message)

        try:
            # CORS 预检直接 204，不进入业务链路
            if method == "OPTIONS":
                await self._json(send_wrapper, 204, None)
                return

            # 限流放在鉴权之前：未登录的刷接口也要被挡住；探针不计数
            if path != "/healthz" and self.ip_per_min > 0:
                ip = resolve_client_ip(
                    (scope.get("client") or ("", 0))[0],
                    _decode(headers, b"x-forwarded-for"),
                    _decode(headers, b"x-real-ip"),
                    self.cfg.trusted_proxies,
                )
                limiter = self._limiter_getter()
                if limiter is not None and not await limiter.allow(
                    "ip", ip, self.ip_per_min, 60.0
                ):
                    raise ApiError(CODE_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")

            wrapped_receive = receive
            if path.startswith("/api/v1"):
                limit, message = _body_rule(path)
                wrapped_receive = _limited_receive(receive, limit, message)

            await self.app(scope, wrapped_receive, send_wrapper)

        except ApiError as exc:
            if not state["sent"]:
                await self._json(
                    send_wrapper, http_status(exc.code), body(exc.code, exc.message, None)
                )
        except Exception:
            logger.exception("rid=%s panic", rid)
            if not state["sent"]:
                await self._json(
                    send_wrapper,
                    http_status(CODE_INTERNAL),
                    body(CODE_INTERNAL, "服务内部错误", None),
                )
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "rid=%s %s %s status=%d latency=%.2fms",
                rid,
                method,
                path,
                state["status"],
                elapsed_ms,
            )
            request_id_var.reset(token)

    @staticmethod
    async def _json(send: Send, status: int, payload: dict[str, Any] | None) -> None:
        raw = (
            b""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(raw)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": raw, "more_body": False})


def _body_rule(path: str) -> tuple[int, str]:
    for suffixes, limit, message in _BODY_RULES:
        if path.endswith(suffixes):
            return limit, message
    return API_BODY_LIMIT, "请求体格式错误"


def _limited_receive(receive: Receive, max_bytes: int, message: str) -> Receive:
    """累计请求体字节数，超限抛 400（对齐 Go 的 MaxBytesReader + 绑定失败语义）。"""

    total = 0

    async def wrapped() -> Message:
        nonlocal total
        item = await receive()
        if item["type"] == "http.request":
            total += len(item.get("body", b""))
            if total > max_bytes:
                raise ApiError(CODE_BAD_REQUEST, message)
        return item

    return wrapped
