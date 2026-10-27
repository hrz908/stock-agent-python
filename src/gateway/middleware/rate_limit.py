"""基于 Redis 有序集合的滑动窗口限流。

与 Go 版 1:1 对齐的语义（这几条是踩过坑的）：
- 「放行才写入」：被 429 的请求不占窗口容量；
- 读-判-写在一段 Lua 内完成，杜绝并发竞态；
- member 必须唯一，否则同毫秒内多次请求会被 ZADD 覆盖而漏计；
- Redis 故障 fail-open：限流是旁路保护，不能因 Redis 抖动打断业务；
- /healthz 不参与计数（探针几秒一次，误伤会导致编排误判）。

三档互不共享窗口：ip（全局，鉴权前）/ login（登录接口）/ user（行情·搜索，按 uid）。
"""

from __future__ import annotations

import itertools
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis
from fastapi import Request

from gateway.infra.context import current_request_id
from gateway.infra.resp import CODE_TOO_MANY_REQUESTS, ApiError
from gateway.middleware.clientip import client_ip

logger = logging.getLogger("stocka.gateway.ratelimit")

WINDOW_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window_ms)
local cnt = redis.call('ZCARD', key)
local ttl = math.ceil(window_ms / 1000)
if cnt >= limit then
    redis.call('EXPIRE', key, ttl)
    return 0
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, ttl)
return 1
"""

_seq = itertools.count(1)


class SlidingWindowLimiter:
    """处理器在启动时构造一次，不能每请求新建（会重复分配 seq 与脚本对象）。"""

    def __init__(self, redis: aioredis.Redis | None) -> None:
        self._redis = redis
        self._script = redis.register_script(WINDOW_SCRIPT) if redis is not None else None

    async def allow(self, name: str, ident: str, limit: int, window_seconds: float) -> bool:
        """返回是否放行；Redis 出错时放行（fail-open）。"""
        if self._redis is None or self._script is None or limit <= 0:
            return True
        now_ms = int(time.time() * 1000)
        # member 唯一性：毫秒 + 进程内序号 + uuid，三者任一即可去重，叠加更稳
        member = f"{now_ms}-{next(_seq):x}-{uuid.uuid4().hex}"
        key = f"rl:{name}:{ident}"
        try:
            result = await self._script(
                keys=[key],
                args=[now_ms, int(window_seconds * 1000), int(limit), member],
            )
        except Exception as exc:  # noqa: BLE001 - 旁路逻辑，一律 fail-open
            logger.warning(
                "rid=%s rate limit error, skip: %s", current_request_id(), exc
            )
            return True
        return int(result) != 0


def _too_many() -> ApiError:
    return ApiError(CODE_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")


def make_ip_limit_dependency(
    limiter_getter: Callable[[], SlidingWindowLimiter],
    limit: int,
    window_seconds: float,
    trusted_proxies_getter: Callable[[], str],
    name: str = "ip",
) -> Callable[[Request], Awaitable[None]]:
    """构造一个路由级依赖：按客户端 IP 限流。"""

    async def dependency(request: Request) -> None:
        ip = client_ip(request, trusted_proxies_getter())
        if not await limiter_getter().allow(name, ip, limit, window_seconds):
            raise _too_many()

    return dependency


def make_user_limit_dependency(
    limiter_getter: Callable[[], SlidingWindowLimiter],
    limit: int,
    window_seconds: float,
    trusted_proxies_getter: Callable[[], str],
) -> Callable[[Request], Awaitable[None]]:
    """按登录用户限流；取不到 user_id 时回退按 IP（公开路由也有保护）。"""

    async def dependency(request: Request) -> None:
        uid = getattr(request.state, "user_id", None)
        if isinstance(uid, int) and uid > 0:
            ident = f"u:{uid}"
        else:
            ident = f"ip:{client_ip(request, trusted_proxies_getter())}"
        if not await limiter_getter().allow("user", ident, limit, window_seconds):
            raise _too_many()

    return dependency
