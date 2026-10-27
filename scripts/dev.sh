#!/usr/bin/env bash
# 溜来（stock-agent）常用任务入口
#
# 环境：Git Bash / MSYS2（Windows）或任意 bash
# 用法：bash scripts/dev.sh <命令>      # 不用 chmod 也能跑
#      bash scripts/dev.sh help       # 看全部命令
#
# 为什么不用 Makefile：这台机器上 make 不可用；而且 Makefile 配方行必须以 tab
# 开头，编辑器格式化或跨系统拷贝常把它换成空格，直接报 missing separator。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$ROOT/server"
MINIAPP_DIR="$ROOT/stock-agent-miniapp"
DB_NAME="stock_agent"

# Windows 的 venv 在 Scripts/，Unix 在 bin/
VENV_PY="$ROOT/.venv/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY="$ROOT/.venv/Scripts/python.exe"

usage() {
  cat <<'EOF'
溜来 · 常用命令（bash scripts/dev.sh <命令>）

基础设施
  up            起依赖：postgres + redis（等 healthy 再 dev）
  down          停依赖（数据保留在卷里）
  ps            容器状态
  logs          依赖容器日志
  psql          进数据库
  redis         进 Redis（限流 rl:* 、日配额 rl:quota:*）

单服务（网关 + agent 同进程，跑在宿主机）
  dev           起服务（--reload，改代码自动重启）
  serve         起服务（无 reload，贴近生产的单进程）
  test          契约冒烟（信封 / 鉴权 / 限流 / SSE 帧格式）
  lint          ruff check
  fmt           ruff format
  migrate       把 server/migrations/*.sql 依次灌进库（新库初始化用）

评测/压测
  bench [cmd]   评测与压测统一入口（bash scripts/dev.sh bench help）

小程序
  miniapp       npm install（换机器首次必做）
  fix-npm       「构建 npm」之后必须跑，否则 PC 端微信白屏

其它
  help          显示本帮助
EOF
}

die() { echo "✗ $*" >&2; exit 1; }
need_compose() { command -v docker >/dev/null 2>&1 || die "没找到 docker，先启动 Docker Desktop"; }
need_venv() {
  [[ -x "$VENV_PY" ]] || die "缺少 .venv：python -m venv .venv && .venv/Scripts/pip install -r server/requirements.txt -r agent/requirements.txt"
}

COMPOSE_FILE="$SERVER_DIR/docker-compose.yml"
cmd_up() { need_compose; docker compose -f "$COMPOSE_FILE" up -d; cmd_ps; }
cmd_down() { need_compose; docker compose -f "$COMPOSE_FILE" down; }
cmd_ps() { need_compose; docker compose -f "$COMPOSE_FILE" ps; }
cmd_logs() { need_compose; docker compose -f "$COMPOSE_FILE" logs -f; }
cmd_psql() { need_compose; docker compose -f "$COMPOSE_FILE" exec postgres psql -U postgres -d "$DB_NAME"; }
cmd_redis() { need_compose; docker compose -f "$COMPOSE_FILE" exec redis redis-cli; }

cmd_dev() {
  need_venv
  [[ -f "$SERVER_DIR/.env" ]] || die "缺少 $SERVER_DIR/.env（被 git 忽略），没有它连不上库与 Redis"
  cd "$SERVER_DIR"
  set -a; . ./.env; set +a
  cd "$ROOT"
  exec "$VENV_PY" -m uvicorn server.main:app \
    --host 0.0.0.0 --port "${PORT:-8080}" \
    --reload --reload-dir server --reload-dir agent
}

cmd_serve() {
  need_venv
  [[ -f "$SERVER_DIR/.env" ]] || die "缺少 $SERVER_DIR/.env（被 git 忽略）"
  cd "$SERVER_DIR"
  set -a; . ./.env; set +a
  cd "$ROOT"
  exec "$VENV_PY" -m uvicorn server.main:app \
    --host 0.0.0.0 --port "${PORT:-8080}" --no-proxy-headers
}

cmd_test() { need_venv; (cd "$ROOT" && "$VENV_PY" scripts/smoke_gateway.py); }
cmd_lint() { need_venv; (cd "$ROOT" && "$VENV_PY" -m ruff check server scripts agent); }
cmd_fmt() { need_venv; (cd "$ROOT" && "$VENV_PY" -m ruff format server scripts agent); }

cmd_migrate() {
  need_compose
  for f in "$SERVER_DIR"/migrations/*.sql; do
    echo "→ $(basename "$f")"
    docker compose -f "$COMPOSE_FILE" exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d "$DB_NAME" < "$f" >/dev/null
  done
  echo "✓ migrations applied"
}

cmd_miniapp() { (cd "$MINIAPP_DIR" && npm install); }
cmd_fix_npm() { (cd "$MINIAPP_DIR" && npm run fix:npm); }

cmd="${1:-help}"
case "$cmd" in
  up)             cmd_up ;;
  down)           cmd_down ;;
  ps)             cmd_ps ;;
  logs)           cmd_logs ;;
  psql)           cmd_psql ;;
  redis)          cmd_redis ;;
  dev)            cmd_dev ;;
  serve)          cmd_serve ;;
  test|smoke)     cmd_test ;;
  lint)           cmd_lint ;;
  fmt)            cmd_fmt ;;
  migrate)        cmd_migrate ;;
  bench)          shift; exec bash "$ROOT/scripts/bench.sh" "$@" ;;
  miniapp)        cmd_miniapp ;;
  fix-npm)        cmd_fix_npm ;;
  help|-h|--help) usage ;;
  *)              echo "✗ 未知命令：$cmd" >&2; echo; usage; exit 1 ;;
esac
