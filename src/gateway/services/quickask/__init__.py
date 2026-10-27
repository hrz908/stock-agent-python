"""Quick Ask：首屏引导问题（业务层）。

三个模块与职责一一对应：
- pool     ：候选池（成交额榜 + 站内关注度 + 行情打分 → Redis ZSET）
- sample   ：热度加权抽样 + 板块多样性 + 会话去重
- template ：按市场阶段与个股状态选模板拼问句

对外两个接口在 `server/api/v1/quickask.py`。
"""
