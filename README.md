# 溜来 · stock-agent-rag

股票问答 Agent 多服务仓库（monorepo）。微信小程序提问 → Go 网关鉴权/限流/代理 → Python Agent（LangGraph + RAG + MCP 工具）检索与生成 → pgvector / Redis 持久化。

## 仓库结构

| 目录 | 技术栈 | 职责 |
|---|---|---|
| `agent/` | Python (FastAPI + LangGraph) | 问答核心：意图路由、RAG 漏斗、子图（stock/chat/rag）、工具与 MCP |
| `server/` | Go (gin + gorm) | API 网关：JWT 鉴权、限流、行情/自选/预警/用户、A股主数据（搜索兜底/资料）等业务模块、Agent 流式代理 |
| `ingest/` | Python | 文档入库管线：加载 → 切分 → 向量化 → 写入 pgvector；`ashare/` 为 A股语料采集与主数据同步（股票清单/行业/概念） |
| `stock-agent-miniapp/` | 微信小程序 | 前端：对话、行情、自选、预警、语音输入 |
| `deploy/` | Docker Compose + nginx | 生产编排（`docker-compose.prod.yml`）、nginx 反代、`.env.production.example` |
| `scripts/` | bash | `dev.sh`（本地任务入口）、`deploy.sh`（生产部署）、`backup.sh`（数据库备份/恢复演练）、`watchdog.sh`（探活告警） |
| `docs/` | Markdown | 架构与决策文档 |

## 快速开始（本地）

```bash
# 起依赖：postgres + redis + agent（dev.sh 封装）
bash scripts/dev.sh up
# 起 Go 网关（需 server/.env）
bash scripts/dev.sh dev
# 小程序：微信开发者工具导入 stock-agent-miniapp/，npm install 后「构建 npm」
```

详见 `server/README.md`、`deploy/README.prod.md` 与各服务内说明。

## 架构

跨服务数据流与分层见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

数据分层：结构化数值与主数据进 PostgreSQL 业务表，非结构化文本进 pgvector（`chunks`），
实时行情只走网关 Redis 30s 快照、不落库。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 服务拓扑、分层与数据持久化 |
| [`docs/ASHARE_VECTORIZATION.md`](docs/ASHARE_VECTORIZATION.md) | A股文本语料：采集、清洗、分块、向量化 |
| [`docs/ASHARE_BUSINESS_SCHEMA.md`](docs/ASHARE_BUSINESS_SCHEMA.md) | A股业务表设计（主数据/财务指标）与建表取舍 |
| [`server/README.md`](server/README.md) | 接口总表、卡片契约、业务规则 |
| [`deploy/README.prod.md`](deploy/README.prod.md) | 生产部署与运维手册 |
