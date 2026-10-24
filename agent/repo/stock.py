"""股票主数据查询（迁移 008 的 stock_basic / stock_industry）。

只做 agent 侧真正需要的一件事：给同业对比补候选标的。
代码格式固定 6 位裸码（与 ashare_documents.stock_codes 一致），转换由调用方负责。
"""
from db import fetch_all

# 行业按「东财优先、否则最新一条」取：行业主源是东财（std='em'），
# 东财被封时 ashare 会退新浪（std='sina'），写死 em 会让同业工具整片空掉
_INDUSTRY_LATERAL = """
LATERAL (
  SELECT l1 FROM stock_industry
  WHERE code = b.code
  ORDER BY (std = 'em') DESC, updated_at DESC
  LIMIT 1
) i ON TRUE
"""

# 本股 + 行业：找不到股票或没有行业时后续查询自然返回空，不用异常表达「没数据」
_ME_SQL = f"""
SELECT b.code, b.name, b.market, COALESCE(i.l1, '') AS industry_l1
FROM stock_basic b
LEFT JOIN {_INDUSTRY_LATERAL}
WHERE b.code = %(code)s
"""

# 同行业候选：默认排除 ST（同业对比里放 ST 股会误导），按代码序保证结果稳定
_PEERS_SQL = f"""
SELECT b.code, b.name, b.market
FROM stock_basic b
JOIN {_INDUSTRY_LATERAL}
WHERE i.l1 = %(industry)s
  AND b.code <> %(code)s
  AND b.status = 'listed'
  AND (%(include_st)s OR NOT b.is_st)
ORDER BY b.is_st, b.code
LIMIT %(limit)s
"""


async def industry_peers(code: str, limit: int = 6, include_st: bool = False) -> dict:
    """同行业候选（一级行业；东财优先，东财不可用时为新浪分类）。

    返回 {"code","name","market","industry","peers":[{code,name,market}]}；
    股票不在表里、或它还没有行业时 peers 为空——这是数据状态，不是错误。
    """
    rows = await fetch_all(_ME_SQL, {"code": code})
    if not rows:
        return {"code": code, "name": "", "market": "", "industry": "", "peers": []}

    me = rows[0]
    industry = me.get("industry_l1") or ""
    peers: list[dict] = []
    if industry:
        fresh = await fetch_all(_PEERS_SQL, {
            "industry": industry,
            "code": code,
            "include_st": include_st,
            "limit": max(1, min(int(limit), 20)),
        })
        peers = [{"code": r["code"], "name": r["name"], "market": r["market"]} for r in fresh]

    return {
        "code": me["code"],
        "name": me["name"],
        "market": me["market"],
        "industry": industry,
        "peers": peers,
    }
