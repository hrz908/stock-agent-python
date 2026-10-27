"""对话的三道限流闸门（QPS 令牌桶 / 并发 / 日配额）。

与 Go 版 internal/agent/limiter.go 的 Lua 逐行对齐，因为这四条语义少一条就会出事故：
- 并发被拒要**立即回滚**已占的槽；
- 日配额被拒要**回滚上面已占的并发槽**，否则泄漏一个额度；
- 归还并发槽必须**脱离请求上下文**（客户端断开后 ctx 已取消，不归还就等于把用户锁死）；
- Redis 故障一律 fail-open。

判定发生在写 SSE 头之前，所以对外是标准 JSON 429，而非流内 error 事件——
前端与监控据此区分「被限流」和「流内报错」。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis

from server.context import current_request_id

logger = logging.getLogger("stocka.server.agent_limiter")

ERR_RATE_LIMIT = "rate_limit"
ERR_TOO_MANY_CONCURRENT = "too_many_concurrent"
ERR_QUOTA_EXCEEDED = "quota_exceeded"

CONC_KEY_TTL = 3600
CST = timezone(timedelta(hours=8))

QPS_SCRIPT = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = 1

local data = redis.call('HMGET', key, 'tokens', 'last')
local tokens = tonumber(data[1]) or capacity
local last = tonumber(data[2]) or now

local elapsed = math.max(0, now - last)
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens < requested then
    redis.call('HMSET', key, 'tokens', tokens, 'last', now)
    redis.call('EXPIRE', key, 10)
    return 0
end

tokens = tokens - requested
redis.call('HMSET', key, 'tokens', tokens, 'last', now)
redis.call('EXPIRE', key, 10)
return 1
"""

CONC_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count > tonumber(ARGV[1]) then
    redis.call('DECR', KEYS[1])
    return 0
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"""

QUOTA_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
if count > tonumber(ARGV[2]) then
    redis.call('DECR', KEYS[1])
    return 0
end
return 1
"""


def seconds_to_midnight() -> int:
    """距当日 24:00 的秒数：配额键跨天自动清零，无需定时清理任务。"""
    now = datetime.now(CST)
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    seconds = int((midnight - now).total_seconds())
    # 边界保护：恰在午夜时至少给 1 秒，避免 EXPIRE 0 立即删除
    return seconds if seconds > 0 else 1


class AgentLimiter:
    def __init__(
        self,
        redis: aioredis.Redis | None,
        qps: int,
        concurrent: int,
        daily_quota: int,
    ) -> None:
        self._redis = redis
        self._qps = max(1, qps)
        self._concurrent = max(1, concurrent)
        self._daily_quota = max(1, daily_quota)
        if redis is not None:
            self._qps_script = redis.register_script(QPS_SCRIPT)
            self._conc_script = redis.register_script(CONC_SCRIPT)
            self._quota_script = redis.register_script(QUOTA_SCRIPT)
        else:
            self._qps_script = self._conc_script = self._quota_script = None

    @property
    def _enabled(self) -> bool:
        return self._redis is not None and self._qps_script is not None

    async def allow(self, uid: int) -> str | None:
        """依次过三道闸门；全部通过返回 None，此时已占一个并发槽。

        调用方必须在请求结束后归还（release_concurrent）。
        """
        if not self._enabled:
            return None

        import time

        # 闸门一：QPS 令牌桶（速率与桶容量同值，即 burst = QPS）
        now_sec = time.time()
        try:
            allowed = await self._qps_script(
                keys=[f"rl:qps:{uid}"],
                args=[self._qps, self._qps, now_sec],
            )
            if int(allowed) == 0:
                return ERR_RATE_LIMIT
        except Exception as exc:  # noqa: BLE001 - fail-open
            logger.warning(
                "rid=%s agent limiter qps error (fail-open): %s",
                current_request_id(),
                exc,
            )

        # 闸门二：并发计数（成功后占槽，需配对归还）
        conc_key = f"rl:conc:{uid}"
        try:
            allowed = await self._conc_script(
                keys=[conc_key], args=[self._concurrent, CONC_KEY_TTL]
            )
            if int(allowed) == 0:
                return ERR_TOO_MANY_CONCURRENT
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "rid=%s agent limiter concurrent error (fail-open): %s",
                current_request_id(),
                exc,
            )

        # 闸门三：日配额；被拒时要回滚上面已占的并发槽
        quota_key = f"rl:quota:{uid}:{datetime.now(CST):%Y-%m-%d}"
        try:
            allowed = await self._quota_script(
                keys=[quota_key], args=[seconds_to_midnight(), self._daily_quota]
            )
            if int(allowed) == 0:
                await self._redis.decr(conc_key)
                return ERR_QUOTA_EXCEEDED
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "rid=%s agent limiter quota error (fail-open): %s",
                current_request_id(),
                exc,
            )

        return None

    async def release_concurrent(self, uid: int) -> None:
        """归还一个并发槽。

        用独立超时、不依赖请求上下文：客户端断开后请求 ctx 往往已取消，
        但归还必须执行，否则计数只增不减，用户会被自己的僵尸计数锁死。
        """
        if self._redis is None:
            return
        try:
            await self._redis.decr(f"rl:conc:{uid}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent limiter release concurrent error: %s", exc)
