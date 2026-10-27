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
本地 `upstream` 指向原仓库，需要时可以 `git pull upstream main` **手动**合并。

## 仓库结构

```
stock-agent-python/
├─ src/                    # 四个 Python 包，靠 PYTHONPATH=src 解析（无运行期 sys.path 魔法）
│  ├─ gateway/             #   HTTP 层 + 业务 + 数据访问（FastAPI）
│  ├─ agent/               #   LangGraph 问答核心（意图路由 / RAG / 子图 / 工具层）
│  ├─ ingest/              #   文档与 A股语料入库管线（独立镜像）
│  └─ shared/              #   跨包共用：中文分词（查询侧与入库侧必须同一份）
├─ sql/
│  ├─ bootstrap.sql        #   网关自管三张表的幂等 DDL（启动时执行）
│  └─ migrations/          #   其余建表 SQL（initdb 仅在空数据卷执行一次）
├─ tests/                  # contract / e2e / integration / regression / benchmark
├─ scripts/                # dev.sh 统一入口 + 部署 / 备份 / 探活
├─ deploy/                 # docker / compose / nginx 三类资产 + 运维手册
├─ docs/                   # 架构、接口手册、本地环境、A股语料与主数据
├─ stock-agent-miniapp/    # 微信小程序（前端）
├─ pyproject.toml          # ruff / pyright / pytest 唯一配置
└─ requirements/           # gateway / agent / ingest / ingest-embed / dev 五份清单
```

各目录职责的细节见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 快速开始（本地）

```bash
# 1) 起依赖：postgres + redis
bash scripts/dev.sh up

# 2) 装依赖并起服务（单进程 = 网关 + agent）
bash scripts/dev.sh setup   # 建 .venv（默认 Python 3.11，对齐线上镜像）+ 装 requirements/dev.txt
bash scripts/dev.sh dev     # 已有 .venv 的机器直接跑这句

# setup 优先用 uv（并行下载 + 全局缓存，几秒）；没装 uv 则退化为 venv 自带的 pip（分钟级）。
# 想换解释器版本：PY_VERSION=3.12 bash scripts/dev.sh setup（删掉 .venv 才生效）。

# 3) 契约冒烟（不依赖 DB/Redis，覆盖信封 / 鉴权 / 限流 / SSE 帧 / Quick Ask）
bash scripts/dev.sh test

# 4) 小程序：bash scripts/dev.sh miniapp（= npm install，已装则 1 秒内返回）
#    再用微信开发者工具导入 stock-agent-miniapp/，「工具 → 构建 npm」生成 miniprogram_npm/
```

> **终端统一用 Git Bash**（本机默认 shell）。别在 PowerShell 里直接写 `bash scripts/dev.sh`：
> Windows 的 `bash.exe` 是 WSL 启动器，WSL 里缺 `/bin/bash` 时只会报 `execvpe(/bin/bash) failed`。
> `psql` / `redis` 是交互式命令，mintty 窗口里 `dev.sh` 会自动套 `winpty`。

新电脑先跑 `bash scripts/install-hooks.sh`（钩子 + `remote.prod` + 别名，见 `docs/LOCAL_SETUP.md`）。

**必需的环境变量**（写在仓库根的 `.env`，被 git 忽略；`dev.sh dev` 会自动 source）：

```bash
cp .env.example .env    # 本地模板，填 OPENAI_API_KEY 即可跑通问答
```

| 变量 | 说明 |
|---|---|
| `JWT_SECRET` | 登录 token 签名密钥；`ENV=prod` 时仍是默认值会拒绝启动 |
| `DSN` / `REDIS_ADDR` | 默认指向本地 `localhost:5432` / `127.0.0.1:6379` |
| `OPENAI_API_KEY` / `OPENAI_API_BASE` / `LLM_MODEL` | **问答必需**，缺了对话会返回 `error` 事件 |
| `WECHAT_APPID` / `WECHAT_SECRET` | 小程序登录；都不配时 dev 退化为演示账号（`ENV=prod` 拒绝启动） |
| `WECOM_*` | 提醒推到企业微信；不配则只落站内 |

完整清单与各接口契约见 [`docs/API.md`](docs/API.md)。

### 整镜像跑（与生产一致）

```bash
bash scripts/dev.sh up                                        # 只起 pg + redis
docker build -f deploy/docker/gateway.Dockerfile -t stock-agent-python:local .
docker run --rm --network stock-agent-python_default -p 8080:8080 \
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
FastAPI 单进程 (src/gateway)
   ├─ 全局中间件：Recovery / RequestID / CORS / IP 限流 / 访问日志 / 分接口 body 限制
   ├─ api/v1 → services → repositories → infra（单向依赖）
   ├─ adapters/quote：行情 / K线 / 搜索；adapters/wechat、wecom：外部通道
   └─ adapters/agent_bridge：同进程加载 src/agent
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
3. **少一份运行时**：原双容器（448M + 1088M）合并为一个（限额 1400M）。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/API.md`](docs/API.md) | 接口总表、卡片契约、业务规则、环境变量、运行方式 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 服务拓扑、分层与目录职责、数据持久化、部署形态 |
| [`docs/LOCAL_SETUP.md`](docs/LOCAL_SETUP.md) | 新机器环境准备（Git Bash、uv、钩子、remote） |
| [`docs/ASHARE_VECTORIZATION.md`](docs/ASHARE_VECTORIZATION.md) | A股文本语料：采集、清洗、分块、向量化 |
| [`docs/ASHARE_BUSINESS_SCHEMA.md`](docs/ASHARE_BUSINESS_SCHEMA.md) | A股业务表设计（主数据/财务指标）与建表取舍 |
| [`deploy/README.md`](deploy/README.md) | 生产部署与运维手册 |

## 对外契约

24 条业务接口已从 Go 版 1:1 迁移完成并通过契约冒烟：

- 鉴权与会话：微信登录、JWT、用户资料/头像/注销
- 行情：搜索（腾讯 smartbox + 主数据兜底）、行情详情、K线、主数据
- 自选：添加（先拉行情再写库）、列表（盈亏/涨跌排序）、刷新、软删除
- 提醒：CRUD + 触发检查（防抖锁、收盘确认、企业微信双通道）
- 快捷提问：热度池 + 多样性抽样 + 模板拼句
- 对话：SSE 流式 + 历史会话（同进程直驱 LangGraph）

契约细节不变：`{code,message,data}` 信封、HTTP 401 触发小程序静默重登、SSE 帧格式
`event: <type>\ndata: <json>\n\n`、金额一律字符串 + Decimal 计算。
