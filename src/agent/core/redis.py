"""进程级共享 Redis 客户端，由 lifespan 统一收尾关闭。"""
import redis.asyncio as aioredis

from agent.config import REDIS_URL

_client = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
