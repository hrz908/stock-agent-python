# 溜来 · stock-agent-python

微信小程序「溜来」的后端与 Agent —— **全 Python 单进程**。
小程序提问 → FastAPI（JWT 鉴权 / 限流 / 行情·自选·提醒等业务 + LangGraph 问答）→ pgvector / Redis。

## 与 stock-agent-rag 的关系

本仓库由 [`stock-agent-rag`](https://github.com/hrz908/stock-agent-rag) 演进而来，
**两者是独立仓库**，不再共享代码改动：

| | `stock-agent-rag`（原） | `stock-agent-python`（本仓库） |
|---|---|---|
| 后端形态 | Go 网关容器 + Python Agent 容器 | 单一 Python 进程 |
| 对话链路 | 网关 POST agent 的 ndjson 流 → 逐行转 SSE | 进程内直接驱动事件生成器 → SSE |
| Agent 取行情 | HTTP 回调网关，带 `GO_SERVER_TOKEN` | 同进程直调业务函数，无需服务 token |
| 后端容器数 | 2 | 1 |
| 网关实现 | Go（gin + gorm） | Python（FastAPI + psycopg3） |

git 历史同源：本仓库早期提交来自 `stock-agent-rag`，之后独立演进。
本地 `upstream` 指向原仓库，需要时可以 `git pull upstream main` **手动**合并，
但不会自动同步 —— 两边的新功能需要各自实现。

## 仓库结构

| 目录 | 技术栈 | 职责 |
|---|---|---|
| `server/` | Python (FastAPI) | 网关 + 业务 + 对话入口：鉴权、限流、行情/自选/提醒/快捷提问、SSE、会话历史 |
| `agent/` | Python (LangGraph) | 问答核心：意图路由、RAG 漏斗、子图（stock/chat/rag）、进程内工具层。**由 `server/agent_bridge.py` 同进程加载，不再单独起服务** |
| `ingest/` | Python | 文档入库管线：加载 → 切分 → 向量化 → 写入 pgvector；`ashare/` 为 A股语料采集与主数据同步 |
| `stock-agent-miniapp/` | 微信小程序 | 前端：对话、行情、自选、预警、语音输入 |
| `deploy/` | Docker Compose + nginx | 生产编排（`docker-compose.prod.yml`，已改为单后端服务）、nginx 反代、`.env.production.example` |
| `scripts/` | bash + Python | `dev.sh`（本地任务入口）、`deploy.sh`（生产部署）、`smoke_gateway.py`（契约冒烟）、`e2e_new_chat.py`、`release.sh`、`install-hooks.sh`、`backup.sh`、`watchdog.sh` |
| `docs/` | Markdown | 架构与决策文档 |

## 快速开始（本地）

```bash
# 1) 起依赖：postgres + redis（网关与 agent 都跑在宿主机）
bash scripts/dev.sh up

# 2) 装依赖并起服务（单进程 = 网关 + agent）
python -m venv .venv
.venv/Scripts/pip install -r server/requirements.txt -r agent/requirements.txt   # Windows
# .venv/bin/pip install -r server/requirements.txt -r agent/requirements.txt     # macOS / Linux
bash scripts/dev.sh dev

# 3) 契约冒烟（不依赖 DB/Redis，覆盖信封 / 鉴权 / 限流 / SSE 帧 / Quick Ask）
bash scripts/dev.sh test

# 4) 小程序：微信开发者工具导入 stock-agent-miniapp/，npm install 后「构建 npm」
```

新电脑先跑 `bash scripts/install-hooks.sh`（钩子 + `remote.prod` + 别名，见 `docs/LOCAL_SETUP.md`）。

**必需的环境变量**（写在 `server/.env`，被 git 忽略；`dev.sh dev` 会自动 source）：

| 变量 | 说明 |
|---|---|
| `JWT_SECRET` | 登录 token 签名密钥；`ENV=prod` 时仍是默认值会拒绝启动 |
| `DSN` / `REDIS_ADDR` | 默认指向本地 `localhost:5432` / `127.0.0.1:6379` |
| `OPENAI_API_KEY` / `OPENAI_API_BASE` / `LLM_MODEL` | **问答必需**，缺了对话会返回 `error` 事件 |
| `WECHAT_APPID` / `WECHAT_SECRET` | 小程序登录；都不配时 dev 退化为演示账号（`ENV=prod` 拒绝启动） |
| `WECOM_*` | 提醒推到企业微信；不配则只落站内 |

完整清单与各接口契约见 [`server/README.md`](server/README.md)。

### 整镜像跑（与生产一致）

```bash
bash scripts/dev.sh up                                        # 只起 pg + redis
docker build -f server/Dockerfile -t stock-agent-python:local .
docker run --rm --network server_default -p 8080:8080 \
  -e ENV=dev -e JWT_SECRET=local-dev-secret-32bytes \
  -e DSN="postgres://postgres:postgres@postgres:5432/stock_agent?sslmode=disable" \
  -e REDIS_ADDR="redis:6379" \
  -e OPENAI_API_KEY=sk-xxx \
  stock-agent-python:local
```

> 国内拉 `python:3.11-slim` 常超时，可先用镜像源拉再打 tag：
> `docker pull docker.m.daocloud.io/library/python:3.11-slim && docker tag docker.m.daocloud.io/library/python:3.11-slim python:3.11-slim`，
> 之后 `docker build --pull=false ...`。

## 架构

```
微信小程序 (stock-agent-miniapp)
        │  HTTPS（宿主机 nginx → 127.0.0.1:8080）
        ▼
FastAPI 单进程 (server/)
   ├─ 全局中间件：Recovery / RequestID / CORS / IP 限流 / 访问日志 / 分接口 body 限制
   ├─ 业务模块：auth · user · watchlist · alert · stock · quickask
   ├─ 对话入口：/api/v1/agent/stream（SSE）+ /api/v1/agent/sessions*（历史会话）
   └─ agent_bridge：同进程加载 agent/
        ├─ LangGraph 主图：guardrail → rewrite → classify → 子图（rag / stock / chat）
        ├─ 工具层 tools/：行情/搜索/财务/资金流/龙虎榜/资讯/研报（@tool 直调业务函数）
        └─ 检索 rag/：pgvector 向量 + 全文 RRF 融合 → 交叉编码器重排
        │
        ▼
PostgreSQL(pgvector) + Redis  ← ingest/ 写入文档向量；网关与 agent 共用连接
```

数据分层：结构化数值与主数据进 PostgreSQL 业务表，非结构化文本进 pgvector（`chunks`），
实时行情只走 Redis 30s 快照、不落库。

单进程带来的三处实质变化：

1. **少一跳 HTTP**：原来网关要把 agent 的 ndjson 逐行解析再转 SSE，现在直接消费事件生成器；
2. **少一次自调用鉴权**：agent 工具不再回调网关，`GO_SERVER_URL` / `GO_SERVER_TOKEN` 已作废；
3. **少一份运行时**：原 `server`(448M) + `agent`(1088M) 两个容器合并为一个（限额 1400M）。

## 文档

| 文档 | 内容 |
|---|---|
| [`server/README.md`](server/README.md) | 接口总表、卡片契约、业务规则、环境变量、运行方式 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 分层与数据持久化（**注意：§1 拓扑图仍是 Go 网关时期的双服务版本，单进程拓扑以上面「架构」为准**） |
| [`docs/ASHARE_VECTORIZATION.md`](docs/ASHARE_VECTORIZATION.md) | A股文本语料：采集、清洗、分块、向量化 |
| [`docs/ASHARE_BUSINESS_SCHEMA.md`](docs/ASHARE_BUSINESS_SCHEMA.md) | A股业务表设计（主数据/财务指标）与建表取舍 |
| [`deploy/README.prod.md`](deploy/README.prod.md) | 生产部署与运维手册 |

## 迁移状态

Go 后端已全部替换为 Python，**24 条业务接口 1:1 迁移完成**并通过契约冒烟：

- 鉴权与会话：微信登录、JWT、用户资料/头像/注销
- 行情：搜索（腾讯 smartbox + 主数据兜底）、行情详情、K线、主数据
- 自选：添加（先拉行情再写库）、列表（盈亏/涨跌排序）、刷新、软删除
- 提醒：CRUD + 触发检查（防抖锁、收盘确认、企业微信双通道）
- 快捷提问：热度池 + 多样性抽样 + 模板拼句
- 对话：SSE 流式 + 历史会话（同进程直驱 LangGraph）

对外契约未变：`{code,message,data}` 信封、HTTP 401 触发小程序静默重登、SSE 帧格式
`event: <type>\ndata: <json>\n\n`、金额一律字符串 + Decimal 计算。
