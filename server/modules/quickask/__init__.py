"""Quick Ask：首屏引导问题。

三层结构，与包内模块一一对应：
- pool     ：候选池（成交额榜 + 站内关注度 + 行情打分 → Redis ZSET）
- sample   ：热度加权抽样 + 板块多样性 + 会话去重
- template ：按市场阶段与个股状态选模板拼问句
- api      ：两个对外接口

注意 api 模块不能叫 router：包内 `router` 是导出给 main 装配的 APIRouter 对象，
同名会覆盖模块属性，导致 `quickask.router.load_pool` 这类 monkeypatch 失效。
"""

from server.modules.quickask.api import router

__all__ = ["router"]
