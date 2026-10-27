# 架构说明（Architecture）

> 2026-10-27 更新：后端由「Go 网关 + 独立 Python Agent」双服务改为**单 Python 进程**；
> 同日完成目录收敛（`src` 布局）。对外契约（路径 / 信封 / SSE 帧 / 401 语义）未变。

## 1. 服务拓扑

```
微信小程序 (stock-agent-miniapp)
        │  HTTPS（宿主机 nginx → 127.0.0.1:8080）
        ▼
FastAPI 单进程 (src/gateway)  ── JWT 鉴权 / 限流 / CORS / 访问日志
   ├─ /api/v1/*        业务接口（api → services → repositories）
   └─ /api/v1/agent/*  SSE 流式对话 + 会话历史（不再反向代理）
        │  同进程函数调用（src/gateway/adapters/agent_bridge.py）
        ▼
LangGraph 主图 (src/agent)  ── 与本进程同生共死
   ├─ 意图路由：guardrail → rewrite → classify → route_by_intent
   ├─ 子图：rag（RAG 漏斗）/ stock（进程内工具取数）/ chat
   ├─ 工具层 tools/：行情/搜索/主数据(同业)/财务/资金流/龙虎榜/资讯/历史K线/研报
   │    （@tool **直调 gateway 的业务函数**，不再 HTTP 回调 + 服务 token）
   └─ 检索 rag/retriever：pgvector 向量 + 全文 RRF 融合 → 交叉编码器重排
        → sufficiency_check：重排分阈值 + LLM 判读两级（不足则 escalate 到 stock 子图）
        │
        ▼
PostgreSQL(pgvector) + Redis  ← src/ingest 写入文档向量；网关与 agent 共用连接
```

单进程化的三处实质收益：

| | 合并前 | 合并后 |
|---|---|---|
| 对话链路 | 网关 `POST agent:8000/run/stream` 拿 ndjson，逐行解析再转 SSE | 直接 `async for` 消费 agent 的事件生成器并写 SSE 帧 |
| 工具取数 | agent 带 `Authorization: Bearer $GO_SERVER_TOKEN` 回调网关 | 同进程调用 `gateway.services.stock` 的函数 |
| 运行时 | Go 网关容器（448M）+ Python Agent 容器（1088M） | 单容器，限额 1400M |

## 2. 分层与目录职责

全仓只有一个包根：`src/`（`PYTHONPATH=src`，镜像与本地一致）。四个包互相之间的依赖方向固定：
`gateway → agent`、`ingest → shared`、`agent → shared`；`shared` 不依赖任何业务包。

### `src/gateway/`（HTTP 层）

| 目录 | 职责 | 禁止 |
|---|---|---|
| `main.py` | 应用装配：lifespan（建池 / 建表 / 预热 agent）、异常→信封、路由三档 | 写业务 |
| `api/v1/` | 取参、入参校验、拼响应 | 写 SQL、调 agent 内部模块 |
| `services/` | 业务编排、事务边界、跨模块协作 | 拼 SQL、碰 `Request` |
| `repositories/` | 全部 SQL 与行→对象映射 | 抛 `ApiError`（由 services 翻译） |
| `adapters/` | 进程外/跨包适配：`quote/`、`wechat.py`、`wecom.py`、`agent_bridge.py` | 写业务规则 |
| `infra/` | `config` `resp` `context` `deps` `web` `hook` `collections` `cache` `agent_limiter` `db/` | 依赖上层 |
| `middleware/` | `gateway.py`（纯 ASGI，避免 SSE 被缓冲）、`rate_limit.py`（滑动窗口 Lua）、`guards.py`、`clientip.py` | — |
| `security/` | `jwt.py`（HS256，拒算法混淆）、`auth.py`（user_id 只从 Bearer 解出） | — |
| `domain/` | `amount.py`（金额定点格式化）、`stockcode.py`（代码归一化） | 访问 IO |

### `src/agent/`（LangGraph）

- `core/`：`graph.py`（主图）、`state.py`、`llm.py`、`events.py`（问答事件流）、`checkpointer.py`/`sessions.py`（DAL）、`template.py`（提示词渲染）、`tasktracker.py`
- `rag/`：`retriever.py`（向量 + 全文 RRF）、`reranker.py`、`entity.py`
- `nodes/` `subgraphs/` `tools/`：LangGraph 节点、子图、`@tool` 取数工具
- `prompts/`：`*.xml` 提示词模板
- `storage/`：`read.py`（主数据只读池，会话级 `read_only`）、`app.py`（应用表读写池）、`pool.py`（共用惰性池骨架）
- `repo/`：固定 SQL 的主数据仓储
- `config.py`：全局配置

### `src/ingest/`（独立镜像）

- 根级：通用文档管线 `chunker / loaders / embedder / store / pipeline / watcher`
- `ashare/`：A股语料采集与主数据同步（`cli.py` 为入口，`python -m ingest.ashare.cli`）

### `src/shared/`

- `zh_tokenize.py`：中文 bigram 词元化。**查询侧与入库侧必须是同一份实现**，
  否则 `chunks.search_text` 与查询 `tsquery` 口径分叉，全文检索会静默退化为「查不到」。

## 3. 数据持久化

- **PostgreSQL + pgvector**：对话 checkpoint、会话注册表（`chat_sessions`）、文档向量、业务表（用户/自选/提醒）。
- **网关自管的三张表**（`users` / `user_watchlist` / `alert_rule`）：由 `sql/bootstrap.sql` 在启动时幂等创建，
  是这三张表的**唯一 DDL 来源**；`sql/migrations/` 只放 agent 与主数据侧的表，两者边界不要混。
- **A股文本语料**：向量写 `chunks`，文档级富元数据/去重指纹/采集水位写 `ashare_documents`、
  `ashare_sync_state`、`ashare_ingest_runs`（迁移 007），见 [`ASHARE_VECTORIZATION.md`](ASHARE_VECTORIZATION.md)。
- **A股主数据**：`stock_basic`、`stock_industry`、`stock_concept`、只读视图 `v_stock_profile`（迁移 008）、
  `stock_finance_indicator`（迁移 009），见 [`ASHARE_BUSINESS_SCHEMA.md`](ASHARE_BUSINESS_SCHEMA.md)。
- **Redis**：限流滑动窗口、对话日配额与并发槽、行情 30s 快照、快捷提问候选池与会话去重状态、提醒防抖锁。
- **迁移**：`sql/migrations/*.sql` 顺序执行；改表新增迁移文件，勿改已执行脚本。

## 4. 部署

- 本地：`deploy/compose/dev.yml` **只起 pg + redis**；服务在宿主机跑
  `bash scripts/dev.sh dev`（`uvicorn gateway.main:app`），或整镜像
  `docker build -f deploy/docker/gateway.Dockerfile .`。
- 生产：`deploy/compose/prod.yml` 里**只有一个后端服务 `server`**（构建上下文为仓库根，
  `COPY src/` + `COPY sql/`），内存限额 1400M；nginx 反代规则不变（`upstream` 指向 `127.0.0.1:8080`）。
- 已移除的配置：`agent` 容器、`AGENT_BASE_URL`、`GO_SERVER_URL`、`GO_SERVER_TOKEN`。
  网关与 agent 的数据源统一走 `DSN` / `REDIS_ADDR`，`agent_bridge` 启动时把它们映射给 agent 侧。
- 生产必须设置 `JWT_SECRET` 与微信凭据，否则 `ENV=prod` 启动自检直接退出。
