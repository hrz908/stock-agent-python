#!/usr/bin/env bash
# 溜来（stock-agent）常用任务入口
#
# 环境：Git Bash / MSYS2（Windows）或任意 bash
# 用法：bash scripts/dev.sh <命令>      # 不用 chmod 也能跑
#      bash scripts/dev.sh help       # 看全部命令
#      .\scripts\dev.ps1 <命令>       # PowerShell 用这个：系统 bash.exe 是 WSL 启动器
#
# 为什么不用 Makefile：这台机器上 make 不可用；而且 Makefile 配方行必须以 tab
# 开头，编辑器格式化或跨系统拷贝常把它换成空格，直接报 missing separator。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$ROOT/server"
AGENT_DIR="$ROOT/agent"
MINIAPP_DIR="$ROOT/stock-agent-miniapp"
DB_NAME="stock_agent"
# 与 server/Dockerfile 的 python:3.11-slim 对齐：本地跑 3.13、生产跑 3.11
# 时，asyncio / 标准库行为差异会变成「本地正常、线上复现不了」的那类 bug
PY_TARGET="${PY_VERSION:-3.11}"

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
  setup         装依赖到 .venv（有 uv 走 uv，秒级；没有退化为 pip）
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
  [[ -x "$VENV_PY" ]] || die "缺少 .venv：先跑 bash scripts/dev.sh setup"
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

# 装依赖：有 uv 就走 uv（并行下载 + 全局缓存，通常几秒），没有才退化到 pip（分钟级）。
# 首次建 .venv 时用 uv venv --python 3.11 对齐生产镜像；已有 .venv 不重建，只提示版本差异
# ——重建会让所有人重装一遍依赖，属于该由人决定的动作。
venv_ver() { "$VENV_PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?"; }

cmd_setup() {
  if [[ ! -x "$VENV_PY" ]]; then
    if command -v uv >/dev/null 2>&1; then
      echo "→ uv venv --python $PY_TARGET .venv"
      uv venv --python "$PY_TARGET" "$ROOT/.venv" || die "uv venv 失败"
    elif command -v python >/dev/null 2>&1; then
      echo "→ python -m venv .venv（装了 uv 会快很多）"
      python -m venv "$ROOT/.venv"
    else
      die "既没有 uv 也没有 python，先装一个（uv 见 https://docs.astral.sh/uv/）"
    fi
  else
    local ver; ver="$(venv_ver)"
    [[ "$ver" == "$PY_TARGET" ]] || echo "! 现有 .venv 是 Python $ver，生产是 $PY_TARGET；要对齐就删掉 .venv 重跑 setup"
  fi

  if command -v uv >/dev/null 2>&1; then
    echo "→ uv pip install -r server/requirements.txt -r agent/requirements.txt"
    uv pip install --python "$VENV_PY" \
      -r "$SERVER_DIR/requirements.txt" -r "$AGENT_DIR/requirements.txt" || die "uv pip install 失败"
  else
    echo "→ pip install（分钟级）"
    "$VENV_PY" -m pip install \
      -r "$SERVER_DIR/requirements.txt" -r "$AGENT_DIR/requirements.txt" || die "pip install 失败"
  fi
  echo "✓ 依赖就绪（Python $(venv_ver)）；下一步：bash scripts/dev.sh dev"
}

cmd_fix_npm() { (cd "$MINIAPP_DIR" && npm run fix:npm); }

cmd="${1:-help}"
case "$cmd" in
  up)             cmd_up ;;
  down)           cmd_down ;;
  ps)             cmd_ps ;;
  logs)           cmd_logs ;;
  psql)           cmd_psql ;;
  redis)          cmd_redis ;;
  setup|install)  cmd_setup ;;
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
