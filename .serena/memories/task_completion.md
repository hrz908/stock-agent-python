# 收尾检查清单

## 动了 Python（gateway / agent / shared）
```
bash scripts/dev.sh lint          # ruff check src tests scripts
bash scripts/dev.sh test          # 契约冒烟：信封 / 鉴权 / 限流 / SSE / Quick Ask
PYTHONPATH=src python -c "import gateway.main, agent.core.events"   # 导入自检
```
- 动过数据访问：确认 SQL 落在 `gateway/repositories/`，service 里没有 SQL 字符串
- 动过 agent 事件流：跑 `tests/regression/test_regression.py`（P13/P14 段覆盖 TTFT 与分词）
- 动过会话：跑 `tests/e2e/test_sessions_api.py`

## 动了 ingest
- 入库验证：`psql` 里 `select title, embed_model, chunk_count, status from documents;` / `select count(*) from chunks;`
- 入口是 `python -m ingest.main watch|scan|once <path>`；A股语料是 `python -m ingest.ashare.cli ...`

## 动了小程序
- 微信开发者工具手动编译 + 预览（服务端口已关闭，无法 CLI 自动化）
- 执行过「构建 npm」后不再需要任何 patch 步骤（TDesign 已移除，fix:npm 随之删除）

## 动了部署
- `docker compose -f deploy/compose/dev.yml config -q`（prod 用 `deploy/compose/prod.yml`）
- 镜像：`docker build -f deploy/docker/gateway.Dockerfile .`

## 提交前
- `git status` / `git diff` 自查
- 不要提交：`.env`、`server/tmp_smoke/`、`.playwright-cli/`、`stock-agent-miniapp/miniprogram_npm/`
- 冒烟产物 `server/tmp_smoke/` 已被 gitignore（混进向量库会污染 RAG 检索）
