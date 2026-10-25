# 架构说明（Architecture）

## 1. 服务拓扑

```
微信小程序 (stock-agent-miniapp)
        │  HTTPS
        ▼
Go 网关 (server/)  ──JWT 鉴权 / 限流 / CORS / 访问日志
   ├─ /api/v1/*      业务模块（user/stock/watchlist/alert/auth）
   └─ /api/v1/agent/* 反向代理到 Python Agent（SSE / 会话历史）
        │  HTTP (ndjson 流式)
        ▼
Python Agent (agent/)  ──LangGraph 主图
   ├─ 意图路由：guardrail → rewrite → classify → route_by_intent
   ├─ 子图：rag（RAG 漏斗）/ stock（进程内工具取数）/ chat
   ├─ 工具层 tools/：行情/搜索/主数据(同业)/财务/资金流/龙虎榜/资讯/历史K线/研报（@tool 直连）
   └─ 检索 rag/retriever：pgvector 向量 + 全文 RRF 融合 → 交叉编码器重排
        → sufficiency_check：重排分阈值 + LLM 判读两级（不足则 escalate 到 stock 子图）
        │
        ▼
PostgreSQL(pgvector) + Redis  ← ingest/ 写入文档向量；网关/agent 共用连接
```

## 2. 分层与目录职责

### Python Agent (`agent/`)
- `app/`：FastAPI 入口 (`main.py`)、开发服务器 (`dev_server.py`)
- `core/`：编排与基础设施 —— `graph.py`(主图)、`state.py`(状态)、`llm.py`(模型客户端)、`checkpointer.py`/`sessions.py`(DAL)、`template.py`(提示词渲染)
- `rag/`：检索 `retriever.py` + 纯资源 `prompts/*.xml`
- `nodes/` `tools/` `subgraphs/`：LangGraph 节点、工具、子图
- `db.py` + `repo/`：主数据只读连接池（会话级 `default_transaction_read_only`）与固定 SQL 仓储
- `config.py`：全局配置（被全项目 import）

### Go 网关 (`server/internal/`)
- `app/`（可选装配层）、`router/`：路由组装
- `domain/stockcode/`：纯领域工具（股票代码规范化，**无 IO**）—— 原 `internal/stock`
- 上游客户端：`quote/`（腾讯行情）`wechat/`（code2session）`wecom/`（企业微信）
- `module/`：业务 HTTP 模块（`auth/` `stock/` `user/` `watchlist/` `alert/`），每模块自带 `Register(g)`
- `agent/`：代理 Python Agent（SSE/WS/proxy）
- 基础设施：`config/` `db/` `middleware/` `util/` `resp/` `deps/` `httpclient/` `upload/` `ws/` `hook/` `model/`

> 命名约定：`domain/stockcode`（纯计算）与 `module/stock`（HTTP 业务）职责不同、包名区分，避免历史 `internal/stock` 歧义。

## 3. 数据持久化
- **PostgreSQL + pgvector**：对话 checkpoint、会话注册表（`chat_sessions`）、文档向量、业务表（用户/自选/预警）。
- **A股文本语料**：向量仍写 `chunks`，文档级富元数据/去重指纹/采集水位写 `ashare_documents`、`ashare_sync_state`、`ashare_ingest_runs`（迁移 007），设计与清洗/分块策略见 [`ASHARE_VECTORIZATION.md`](ASHARE_VECTORIZATION.md)。
- **A股主数据**：`stock_basic`（6 位裸码主键 + 生成的市场/板块）、`stock_industry`、`stock_concept`、只读视图 `v_stock_profile`（迁移 008），财务指标表 `stock_finance_indicator`（迁移 009，P1）。agent 经独立只读池查询，网关只读不建表；行情与估值不落库，见 [`ASHARE_BUSINESS_SCHEMA.md`](ASHARE_BUSINESS_SCHEMA.md)。
- **Redis**：限流滑动窗口、Agent 日配额、会话滚动摘要。
- **迁移**：`server/migrations/*.sql` 顺序执行；改表新增迁移文件，勿改已执行脚本。

## 4. 部署
- 本地：`server/docker-compose.yml`（pg/redis/agent，端口映射宿主机）。
- 生产：`deploy/docker-compose.prod.yml`（含 ingest profile），nginx 反代走 `deploy/nginx.prod.conf`，密钥来自 `deploy/.env`（模板 `.env.production.example`）。
- Agent 回调网关地址经 `GO_SERVER_URL` 注入；容器内用 compose 网络 DNS（`server`/`agent`），本地用 `host.docker.internal`。
