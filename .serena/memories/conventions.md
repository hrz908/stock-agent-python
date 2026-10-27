# 代码约定

## 通用
- 依赖方向固定：`api/v1 → services → repositories → infra`；`adapters/` 是外部系统与跨包边界的唯一出口
- `repositories/` 是**所有 SQL 的唯一去处**；`services/` 不拼 SQL、不碰 `Request`
- `ApiError` 在 services 抛、api 层只负责取参与校验；DB 异常在 services 里翻译成业务错误码
- 金额一律 `Decimal` + 字符串下发，禁止 float 参与计算
- 跨模块联动走 `infra/hook.py` 事件，模块之间不要互相 import

## 导入与包根（2026-10-27 起）
- 全仓唯一包根 `src/`：`gateway` / `agent` / `ingest` / `shared` 四个包一律绝对导入
- **禁止**在业务代码里 `sys.path.insert`（历史遗留已在重构中删除）；本地靠 `PYTHONPATH=src`，镜像同理
- agent 的工具层要调网关业务函数时，只经 `gateway.adapters.agent_bridge`，不要直接 import 网关内部模块

## 迁移与配置
- `sql/migrations/` 只在**数据卷为空时**执行一次；改表必须新增迁移文件，禁止改已有文件
- `sql/bootstrap.sql` 只管网关三张表（users / user_watchlist / alert_rule），是这三张表的唯一 DDL 来源
- 配置：数值型环境变量解析失败**直接终止启动**（不静默回退）；`ENV=prod` 时 JWT_SECRET 或微信凭据仍是默认值则拒绝启动
- `.env` 放仓库根（`load_config` 仍兼容旧的 `src/gateway/.env`、`server/.env`）

## Python agent
- 意图路由：`stock` / `rag` / `chat` / `clarify`（`mcp` 作为意图名已废弃）
- RAG 不得编造；未命中要如实说明；无数据且命中行情关键词时走固定兜底文案
- 行情 / K 线每轮必须重新取数，**禁止沿用历史消息里的数字**
- 工具子图轮次上限 3；无数据不得自由发挥

## 小程序
- 颜色一律用 `styles/token.wxss` 的变量（主强调色 `--accent` #2559EB），**禁用 TDesign 默认主色 #0052D9**；红涨绿跌：`--up` 只表示行情涨跌，删除/退出/校验错误用 `--danger`
- 模块统一 ESM（`import` / `export`），不再混用 `require` / `module.exports`
- 图标一律 `<base-icon type="..." name>`（`components/base/icon`），不要再引 antd-mini
- 昵称输入：`nicknameFocus` 必须镜像真实焦点（收键盘用 `wx.hideKeyboard()`）；`type=nickname` 只在「取微信昵称」期间存在
- 验证走开发者工具 CLI（见 `mem:miniapp/core`），不再要求手工编译

## 提交
- Conventional Commits + 中文正文，scope 用中文模块名：`feat(小程序): `、`fix(gateway): `、`chore: `
