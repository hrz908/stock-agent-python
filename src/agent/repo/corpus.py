"""语料覆盖判定：点名标的「在不在采集范围」「有没有已入库的文档」。

两个信号一起取，因为对应的话术不一样：
- 不在池内（in_pool=false）：这只票没被采集过，要说清范围（成交额前 N）；
- 在池内但没有文档（has_docs=false）：采集还没轮到它（历史回填进行中、或该期报告尚未发布），
  说「暂未收录」比说「不在范围」准确得多。
只按「有没有文档」判会把「还没采完」误报成「不在范围」——回填期间这是常态。

只查 ashare_documents：文件 ingest 的文档没有 stock_codes，不属于个股语料范围。
"""
from agent.storage.read import fetch_all

# 数组重叠走 ashare_docs_codes 的 GIN 索引；每个代码一次索引探测，几个代码的代价可忽略
_COVERAGE_SQL = """
SELECT t.code,
       EXISTS (
           SELECT 1 FROM ashare_documents a
            WHERE a.stock_codes && ARRAY[t.code]::text[]
       ) AS has_docs,
       EXISTS (
           SELECT 1 FROM ashare_universe u WHERE u.code = t.code
       ) AS in_pool
FROM unnest(%(codes)s::text[]) AS t(code)
"""


async def coverage_of(codes: list[str]) -> list[dict]:
    """[{code, has_docs, in_pool}]；传入空列表返回空（不查库）。"""
    cleaned = [c for c in dict.fromkeys(codes or []) if c]
    if not cleaned:
        return []
    return await fetch_all(_COVERAGE_SQL, {"codes": cleaned})
