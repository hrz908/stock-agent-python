"""生产对话链路的 SSE 客户端（供 runner / perf 复用）。

为什么要单独一层：合并单进程后，agent 的 `/run/stream`（ndjson）不再是生产入口，
真正对外的是网关的 `POST /api/v1/agent/stream`（JWT + SSE）。harness 只有打这条路径，
测到的 TTFT / 总耗时 / 限流 / 帧格式才和线上一致。

本模块只做三件事：签 JWT、发请求、按 `event: <type>\\ndata: <json>\\n\\n` 解析。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator

import httpx

STREAM_PATH = "/api/v1/agent/stream"
DEFAULT_JWT_SECRET = "please-change-me"
DEFAULT_TOKEN_TTL = 12 * 3600


class StreamHttpError(RuntimeError):
    """握手阶段就被拒（401 / 429 / 5xx），此时还没有任何 SSE 帧。"""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body


def jwt_secret(explicit: str | None = None) -> str:
    """显式参数 > 环境变量 > 服务端默认值（仅 dev 成立，prod 会拒绝启动）。"""
    return explicit or os.environ.get("JWT_SECRET") or DEFAULT_JWT_SECRET


def sign_token(
    user_id: int, secret: str | None = None, ttl_seconds: int = DEFAULT_TOKEN_TTL
) -> str:
    """与 server/security/jwt.py 同口径：HS256，claims 只有 user_id / iat / exp。

    harness 直接为合成 uid 签 token，不依赖登录接口——dev 环境所有登录都落到同一个
    demo 用户，没法用登录造出多 uid，而限流是按 uid 分片的。
    """
    import jwt as pyjwt

    now = int(time.time())
    claims = {"user_id": int(user_id), "iat": now, "exp": now + int(ttl_seconds)}
    return pyjwt.encode(claims, secret or jwt_secret(), algorithm="HS256")


def stream_url(base_url: str) -> str:
    return base_url.rstrip("/") + STREAM_PATH


def _decode_data(lines: list[str]):
    raw = "\n".join(lines)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


async def iter_sse(response: httpx.Response) -> AsyncGenerator[tuple[str, object], None]:
    """逐帧产出 (event, data)；忽略注释行与 id/retry 字段。"""
    event = ""
    data_lines: list[str] = []
    async for raw in response.aiter_lines():
        line = raw.rstrip("\r")
        if not line:
            if event or data_lines:
                yield event or "message", _decode_data(data_lines)
            event, data_lines = "", []
            continue
        if line.startswith(":"):  # 心跳/注释
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value.strip()
        elif field == "data":
            data_lines.append(value)
    if event or data_lines:
        yield event or "message", _decode_data(data_lines)


async def agent_stream(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    query: str,
    session_id: str,
) -> AsyncIterator[tuple[str, object]]:
    """发一轮问答并流式产出事件。非 200 抛 StreamHttpError（不带 SSE 帧）。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    body = {"query": query, "session_id": session_id}
    async with client.stream(
        "POST", stream_url(base_url), json=body, headers=headers
    ) as response:
        if response.status_code != 200:
            raw = (await response.aread()).decode("utf-8", "replace")
            raise StreamHttpError(response.status_code, raw)
        async for event, data in iter_sse(response):
            yield event, data


def default_timeout() -> httpx.Timeout:
    """读超时要大于服务端 run_timeout(120s)，否则客户端先断、拿不到服务端 error 帧。"""
    return httpx.Timeout(connect=10.0, read=150.0, write=30.0, pool=30.0)
