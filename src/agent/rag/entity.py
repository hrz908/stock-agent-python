"""查询实体识别：把问题里的股票名称/代码映射成代码集合。

只用于 RAG 命中闸的「实体一致性」校验：问题点名了某家公司，但检索结果全是
别家公司的资料时，不能把别家的数据当作答案（实测问「贵州茅台三季报」会命中
一堆其他公司的三季报点评，重排分同样很高）。

名称表进程内缓存 10 分钟；查询失败返回空集合，校验自动跳过，不影响检索主链路。
文件 ingest 的文档没有 stock_codes，不参与该校验（见 sufficiency_check）。

同一轮里 retrieve / sufficiency_check / fast_plan 会拿同一个（改写后的）问句
反复调用 entity_codes：名称表扫描是 O(名称数) 的 Python 子串匹配，按问句缓存
一个短 TTL 的结果，避免一轮多扫两三遍。
"""
import re
import time

from agent.storage.read import get_read_pool

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_NAME_TTL = 600.0

_names_cache: list[tuple[str, str]] = []
_names_at = 0.0

# 问句 -> (写入时刻, 按出现顺序的代码列表)
_mentions_cache: dict[str, tuple[float, list[str]]] = {}
_MENTION_TTL = 60.0
_MENTION_MAX = 256



async def _stock_names() -> list[tuple[str, str]]:
    """[(name, code)]，按名称长度降序（长名优先，避免短名误命中）。"""
    global _names_cache, _names_at
    if _names_cache and time.monotonic() - _names_at < _NAME_TTL:
        return _names_cache
    try:
        pool = await get_read_pool()
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


async def entity_mentions(query: str) -> list[str]:
    """按出现顺序返回问题里点名的股票代码（去重）。

    顺序对对比类问句有意义：先提到的那只是「本股」，后面的是同行。
    """
    q = (query or "").strip()
    if not q:
        return []
    now = time.monotonic()
    hit = _mentions_cache.get(q)
    if hit and now - hit[0] < _MENTION_TTL:
        return list(hit[1])
    hits = [(m.start(), m.group(1)) for m in _CODE_RE.finditer(q)]
    for name, code in await _stock_names():
        idx = q.find(name)
        if idx >= 0:
            hits.append((idx, code))
    hits.sort(key=lambda x: x[0])
    out: list[str] = []
    for _, code in hits:
        if code not in out:
            out.append(code)
    if len(_mentions_cache) >= _MENTION_MAX:
        for k in list(_mentions_cache)[: _MENTION_MAX // 4]:
            _mentions_cache.pop(k, None)
    _mentions_cache[q] = (now, out)
    return list(out)


async def entity_codes(query: str) -> set[str]:
    """问题里明确出现的股票代码/全称 -> 代码集合；识别不出返回空集。"""
    return set(await entity_mentions(query))
