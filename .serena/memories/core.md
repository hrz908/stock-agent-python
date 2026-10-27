# 溜来（stock-agent）· 源码地图

2026-10-27 起为 **src 布局**，全仓只有一个包根 `src/`（`PYTHONPATH=src`）：

- `src/gateway/` FastAPI 单进程后端（HTTP + 业务 + 数据访问）→ `mem:server/core`
- `src/agent/` Python 智能体（LangGraph）→ `mem:agent/core`
- `src/ingest/` 文档与 A股语料入库（独立镜像，compose 里属 `ingest`/`ashare` profile）
- `src/shared/` 跨包共用（中文分词等，查询侧与入库侧必须同一份）
- `stock-agent-miniapp/` 微信小程序（原生 + TDesign）→ `mem:miniapp/core`
- `sql/` `bootstrap.sql`（网关自管三张表，启动时执行）+ `migrations/`（其余表）
- `tests/` contract / e2e / integration / regression / benchmark
- `deploy/` docker / compose / nginx + 运维手册；`scripts/dev.sh` 统一命令入口（本机无 make）

## 全局不变量
- 品牌名「溜来」
- 一套 PostgreSQL(pgvector)+Redis 供全部服务，唯一真源是 `deploy/compose/dev.yml`
- 端口：gateway 8080 / pg 5432 / redis 6379（不再有独立 agent 端口）
- 默认凭据 `postgres:postgres`、库名 `stock_agent`、`sslmode=disable`
- 本机 Windows：**默认终端是 Git Bash**（2026-10-27 起）；`bash scripts/dev.sh` 直接可用，PowerShell 里的 `bash.exe` 是 WSL 启动器，别用
- 导入只靠 `PYTHONPATH=src`：禁止在业务代码里改 `sys.path`（历史遗留已清理）

## 相关
技术选型 `mem:tech_stack`；命令 `mem:suggested_commands`；约定 `mem:conventions`；收尾 `mem:task_completion`
