# 常用命令

## 统一入口（需 bash，本机无 make）
`bash scripts/dev.sh <cmd>`：`up|down|ps|logs|psql|redis|setup|dev|serve|test|lint|fmt|migrate|bench|miniapp|help`
**默认终端是 Git Bash**，直接这样跑即可。PowerShell 里的 `bash.exe` 是 WSL 启动器，别用。

## 基础设施（Docker）
- 起依赖：`docker compose -f deploy/compose/dev.yml up -d`（postgres/redis，等 healthy 再起服务）
- 停：`docker compose -f deploy/compose/dev.yml down`（数据保留在卷里；`-v` 才删卷）
- 状态/日志：`... ps` / `... logs -f`
- 进库：`... exec postgres psql -U postgres -d stock_agent`
- 进 Redis：`... exec redis redis-cli`
- ingest 属于 profile：`docker compose -f deploy/compose/dev.yml --profile ingest up -d`

## 服务
- 起服务：`bash scripts/dev.sh dev`（= `uvicorn gateway.main:app --reload`，需仓库根 `.env`）
- 无 reload：`bash scripts/dev.sh serve`
- 契约冒烟：`bash scripts/dev.sh test`
- 迁移：`bash scripts/dev.sh migrate`（把 `sql/migrations/*.sql` 灌进库）

## 测试（都在 tests/ 下，PYTHONPATH=src 由脚本/配置保证）
- 契约：`python tests/contract/smoke_gateway.py`（fakeredis[lua]，不依赖真实 PG/Redis）
- 回归：`python tests/regression/test_regression.py`
- 会话：`python tests/e2e/test_sessions_api.py`
- 评测/压测：`bash scripts/bench.sh help`

## 小程序
- `bash scripts/dev.sh miniapp`（= `npm install`）
- 之后在开发者工具里「工具 → 构建 npm」生成 `miniprogram_npm/`（不入库）

## Windows 差异（相对 unix shell）
- Git Bash 里路径用 `/c/...`；`where` → `Get-Command`（仅在 PowerShell 里）
- **mintty 无 Windows 控制台**：`psql` / `redis` 这类 `docker compose exec` 不带 `-T` 会报 `the input device is not a TTY`；`dev.sh` 已自动套 `winpty`，被管道调用时直接报错退出
- winget 装完软件后当前会话 PATH 不刷新，需新开终端
