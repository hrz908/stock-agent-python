# 架构说明（Architecture）

> 2026-10-27 更新：后端已由 Go 网关 + 独立 Python Agent 的双服务形态，改为**单 Python 进程**
> （网关与 agent 同进程）。对外契约（路径 / 信封 / SSE 帧 / 401 语义）未变，
> 变化集中在进程边界与调用方式，见 §1 与 §4。

## 1. 服务拓扑

```
微信小程序 (stock-agent-miniapp)
        │  HTTPS（宿主机 nginx → 127.0.0.1:8080）
        ▼
FastAPI 单进程 (server/)  ──JWT 鉴权 / 限流 / CORS / 访问日志
   ├─ /api/v1/*        业务模块（auth / user / watchlist / alert / stock / quickask）
   └─ /api/v1/agent/*  SSE 流式对话 + 会话历史（不再反向代理）
        │  同进程函数调用（server/agent_bridge.py）
        ▼
LangGraph 主图 (agent/)  ── 与本进程同生共死
   ├─ 意图路由：guardrail → rewrite → classify → route_by_intent
   ├─ 子图：rag（RAG 漏斗）/ stock（进程内工具取数）/ chat
   ├─ 工具层 tools/：行情/搜索/主数据(同业)/财务/资金流/龙虎榜/资讯/历史K线/研报
   │    （@tool **直调 server 的业务函数**，不再 HTTP 回调 + 服务 token）
   └─ 检索 rag/retriever：pgvector 向量 + 全文 RRF 融合 → 交叉编码器重排
        → sufficiency_check：重排分阈值 + LLM 判读两级（不足则 escalate 到 stock 子图）
        │
        ▼
PostgreSQL(pgvector) + Redis  ← ingest/ 写入文档向量；网关与 agent 共用连接
```

合并前后的三处差别（这是本次架构变化的全部收益）：

| | 合并前 | 合并后 |
|---|---|---|
| 对话链路 | 网关 `POST agent:8000/run/stream` 拿 ndjson，逐行解析再转 SSE | 直接 `async for` 消费 agent 的事件生成器并写 SSE 帧 |
| 工具取数 | agent 带 `Authorization: Bearer $GO_SERVER_TOKEN` 回调网关 | 同进程调用 `server.modules.stock` 的函数 |
| 运行时 | Go 网关容器（448M）+ Python Agent 容器（1088M） | 单容器，限额 1400M |

## 2. 分层与目录职责

### 网关 (`server/`)
- `main.py`：应用装配 —— lifespan（建池 / 建表 / 预热 agent）、异常 → 统一信封、路由三档（公开 / 已鉴权 / 行情限流）
- `middleware/`：`gateway.py`（纯 ASGI，避免 SSE 被缓冲）、`rate_limit.py`（滑动窗口 Lua）、`guards.py`（路由级限流依赖）、`clientip.py`（可信反代下的真实 IP）
- `security/`：`jwt.py`（HS256 签发校验，拒绝算法混淆）、`auth.py`（`require_auth`，user_id 只从 Bearer 解出）
- `db/`：`postgres.py`（psycopg3 池，启动必须探活成功）、`redis.py`（连不上降级，不阻断启动）
- `quote/`：`source.py`（腾讯行情/详情，GBK 转码与字段下标）、`cache.py`（30s 快照）、`batch.py`（去重→分批→批间并发）、`search.py`（smartbox）、`kline.py`
- `modules/`：业务 HTTP 模块（`auth` `user` `watchlist` `alert` `stock` `chat` `quickask/`），每个导出 `router`
- `agent_bridge.py`：同进程加载 agent、事件流直驱、会话 CRUD 直调、工具层直调
- `agent_limiter.py`：对话三道闸门（QPS 令牌桶 / 并发 / 日配额）
- `hook.py`：模块间事件（「行情已更新」→ 提醒检查），避免模块横向 import

### Python Agent (`agent/`)
- `app/`：FastAPI 入口 (`main.py`)、开发服务器 (`dev_server.py`)。合并后**不再单独启动**，
  其中的 `stream_events()` 由网关直接调用；`POST /run/stream`（ndjson）仅保留给独立部署/调试
- `core/`：编排与基础设施 —— `graph.py`(主图)、`state.py`(状态)、`llm.py`(模型客户端)、`checkpointer.py`/`sessions.py`(DAL)、`template.py`(提示词渲染)
- `rag/`：检索 `retriever.py` + 纯资源 `prompts/*.xml`
- `nodes/` `tools/` `subgraphs/`：LangGraph 节点、工具、子图
- `db.py` + `repo/`：主数据只读连接池（会话级 `default_transaction_read_only`）与固定 SQL 仓储
- `config.py`：全局配置（被全项目 import）

> 命名约定：`server/domain/stockcode.py`（纯计算）与 `server/modules/stock.py`（HTTP 业务）职责不同，
> 合并前是 `internal/domain/stockcode` 与 `internal/module/stock`。

## 3. 数据持久化
- **PostgreSQL + pgvector**：对话 checkpoint、会话注册表（`chat_sessions`）、文档向量、业务表（用户/自选/提醒）。
- **网关自管的三张表**（`users` / `user_watchlist` / `alert_rule`）：由 `server/schema.sql` 在启动时幂等创建，
  等价于原 Go 版的 gorm AutoMigrate；`migrations/` 里 005 之后的表由 SQL 迁移持有，两者边界不要混。
- **A股文本语料**：向量仍写 `chunks`，文档级富元数据/去重指纹/采集水位写 `ashare_documents`、`ashare_sync_state`、`ashare_ingest_runs`（迁移 007），设计与清洗/分块策略见 [`ASHARE_VECTORIZATION.md`](ASHARE_VECTORIZATION.md)。
- **A股主数据**：`stock_basic`（6 位裸码主键 + 生成的市场/板块）、`stock_industry`、`stock_concept`、只读视图 `v_stock_profile`（迁移 008），财务指标表 `stock_finance_indicator`（迁移 009，P1）。agent 经独立只读池查询；行情与估值不落库，见 [`ASHARE_BUSINESS_SCHEMA.md`](ASHARE_BUSINESS_SCHEMA.md)。
- **Redis**：限流滑动窗口、对话日配额与并发槽、行情 30s 快照、快捷提问候选池与会话去重状态、提醒防抖锁。
- **迁移**：`server/migrations/*.sql` 顺序执行；改表新增迁移文件，勿改已执行脚本。

## 4. 部署
- 本地：`server/docker-compose.yml` **只起 pg + redis**；服务在宿主机跑
  `bash scripts/dev.sh dev`（`uvicorn server.main:app`），或整镜像 `docker build -f server/Dockerfile`。
- 生产：`deploy/docker-compose.prod.yml` 里**只有一个后端服务 `server`**（构建上下文为仓库根，
  一次 COPY `agent/` 与 `server/`），内存限额 1400M；nginx 反代规则不变（`upstream` 仍指向 `127.0.0.1:8080`）。
- 已移除的配置：`agent` 容器、`AGENT_BASE_URL`、`GO_SERVER_URL`、`GO_SERVER_TOKEN`。
  网关与 agent 的数据源统一走 `DSN` / `REDIS_ADDR`，`agent_bridge` 启动时把它们映射给 agent 侧。
- 生产必须设置 `JWT_SECRET` 与微信凭据，否则 `ENV=prod` 启动自检直接退出。
