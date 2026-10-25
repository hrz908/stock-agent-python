"""查询实体识别：把问题里的股票名称/代码映射成代码集合。

只用于 RAG 命中闸的「实体一致性」校验：问题点名了某家公司，但检索结果全是
别家公司的资料时，不能把别家的数据当作答案（实测问「贵州茅台三季报」会命中
一堆其他公司的三季报点评，重排分同样很高）。

名称表进程内缓存 10 分钟；查询失败返回空集合，校验自动跳过，不影响检索主链路。
文件 ingest 的文档没有 stock_codes，不参与该校验（见 sufficiency_check）。
"""
import re
import time

from db import get_pool

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_NAME_TTL = 600.0

_names_cache: list[tuple[str, str]] = []
_names_at = 0.0


async def _stock_names() -> list[tuple[str, str]]:
    """[(name, code)]，按名称长度降序（长名优先，避免短名误命中）。"""
    global _names_cache, _names_at
    if _names_cache and time.monotonic() - _names_at < _NAME_TTL:
        return _names_cache
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT code, name FROM stock_basic "
                    "WHERE status = 'listed' AND length(name) >= 3"
                )
                rows = await cur.fetchall()
        _names_cache = sorted(
            ((str(n), str(c)) for c, n in rows if n), key=lambda x: -len(x[0])
        )
        _names_at = time.monotonic()
    except Exception:  # noqa: BLE001 - 名称表不可用时不做实体校验
        _names_cache = []
    return _names_cache


async def entity_codes(query: str) -> set[str]:
    """问题里明确出现的股票代码/全称 -> 代码集合；识别不出返回空集。"""
    q = (query or "").strip()
    if not q:
        return set()
    codes = set(_CODE_RE.findall(q))
    for name, code in await _stock_names():
        if name in q:
            codes.add(code)
    return codes
