# 技术栈与版本

## Python（全仓）
- 镜像 `python:3.11-slim`；本地 `.venv` 也锁 3.11（`uv venv --python 3.11`）
- 依赖清单集中在 `requirements/`：`gateway.txt`、`agent.txt`、`ingest.txt`、`ingest-embed.txt`、`dev.txt`
- 根 `pyproject.toml` 只放工具配置（ruff / pyright / pytest），**没有 [project]**：三个部署单元都不是可分发包
- ruff：line-length 100（`src/ingest/**` 放宽 E501 到历史 120 列），target py311，规则 E,F,I,UP,B,ASYNC

## gateway
- FastAPI + uvicorn、psycopg[binary,pool]（psycopg3）、redis.asyncio、PyJWT、PyYAML、httpx
- 分层：`api/v1 → services → repositories → infra`，`adapters/` 收外部系统与 agent 桥接

## agent
- LangGraph>=0.2、langchain>=0.3、langchain-openai、langgraph-checkpoint-postgres
- 取数：akshare（东财/新浪公开接口）、baostock（历史日K），pandas；`@tool` 进程内直调，无 MCP 子进程
- tiktoken 做 token 估算；提示词是 `prompts/*.xml`（`core/template.py` 定向替换，无 jinja2）
- 存储：`storage/read.py`（主数据只读池）、`storage/app.py`（应用表读写）、`core/checkpointer.py`（saver）

## ingest
- psycopg、redis、watchdog、tiktoken、pymupdf4llm、pandas、python-docx、python-pptx、openai
- ashare 采集：akshare + requests + beautifulsoup4 + lxml
- 可选本地向量化（`ASHARE_INSTALL_EMBED=1`）：`requirements/ingest-embed.txt`（torch 近 1GB）

## 存储
- PostgreSQL 16 + pgvector（镜像 `pgvector/pgvector:pg16`，`CREATE EXTENSION vector` 依赖它）
- Redis 7 alpine（限流 `rl:*`、日配额 `rl:quota:*`、行情快照 `stock:*`、embedding 缓存 `emb:` TTL 7 天）

## 小程序
- 原生小程序 + `tdesign-miniprogram ^1.17.0`、marked ^4、tinycolor2、tslib

## LLM / 外部
- OpenAI 兼容协议，默认 SiliconFlow：LLM `deepseek-ai/DeepSeek-V3`、small `Qwen/Qwen2.5-7B-Instruct`
- embedding **1536 维**（`EMBED_DIM`），agent 与 ingest 必须一致，否则写入和检索落在两个向量空间
- 行情内置腾讯财经批量接口（`src/gateway/adapters/quote/source.py`）

## 工具链
- ruff（lint/format）、pyright basic、契约冒烟 `tests/contract/smoke_gateway.py`（fakeredis[lua]）
- Serena 已激活本项目，语言服务器 go + python（go 已无用，可忽略）
