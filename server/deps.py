"""进程级依赖容器。

Go 版用 internal/deps 存全局 pg/rdb；Python 侧同样保留一个显式容器，
在 lifespan 里初始化一次，请求期通过 runtime() 取用。

不用模块级裸全局变量是多一层保险：测试可以在同一进程里反复初始化/释放。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis

from server.config import Config
from server.db.postgres import Postgres
from server.middleware.rate_limit import SlidingWindowLimiter

logger = logging.getLogger("stocka.server.deps")


@dataclass
class Runtime:
    cfg: Config
    pg: Postgres
    redis: aioredis.Redis
    limiter: SlidingWindowLimiter
    # 启动探活结果：Redis 连不上时按降级模式跑（限流 fail-open、缓存旁路、
    # Quick Ask 退回进程内兜底池），与 Go 版「连不上不阻断启动」一致
    redis_ok: bool = True
    # 行情源 / 微信 / 企业微信等有状态单例，启动时装配后挂在这里
    services: dict[str, Any] = field(default_factory=dict)

    def service(self, name: str) -> Any:
        try:
            return self.services[name]
        except KeyError as exc:
            raise RuntimeError(f"service {name!r} is not initialised") from exc


_runtime: Runtime | None = None


def set_runtime(rt: Runtime) -> None:
    global _runtime
    _runtime = rt


def runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime is not initialised")
    return _runtime


def config() -> Config:
    return runtime().cfg


async def clear_runtime() -> None:
    global _runtime
    _runtime = None
