#!/usr/bin/env bash
# 溜来（stock-agent）常用任务入口
#
# 环境：Git Bash / MSYS2（Windows）或任意 bash
# 用法：bash scripts/dev.sh <命令>      # 不用 chmod 也能跑
#      bash scripts/dev.sh help       # 看全部命令
# 只在 Git Bash 里跑：Windows 系统自带的 bash.exe 是 WSL 启动器，缺 /bin/bash 时一行都跑不到
#
# 为什么不用 Makefile：这台机器上 make 不可用；而且 Makefile 配方行必须以 tab
# 开头，编辑器格式化或跨系统拷贝常把它换成空格，直接报 missing separator。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT/src"
ENV_FILE="$ROOT/.env"
MINIAPP_DIR="$ROOT/stock-agent-miniapp"
DB_NAME="stock_agent"
# 与 deploy/docker/gateway.Dockerfile 的 python:3.11-slim 对齐：本地跑 3.13、生产跑 3.11
# 时，asyncio / 标准库行为差异会变成「本地正常、线上复现不了」的那类 bug
# 现状（2026-10-27 起）：.venv 由 `uv venv --python 3.11` 创建，pyvenv.cfg 里有 uv 标记；
# 看到 version = 3.13 说明是旧的 python -m venv 环境，删掉重跑 setup
PY_TARGET="${PY_VERSION:-3.11}"
# 生产依赖 + 本地才用的 ruff：一份清单，setup 只装这一条
DEV_REQS="$ROOT/requirements/dev.txt"
# src 布局：gateway / agent / ingest / shared 四个包都在 src/ 下
export PYTHONPATH="$SRC_DIR${PYTHONPATH:+:$PYTHONPATH}"

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
  migrate       把 sql/migrations/*.sql 依次灌进库（新库初始化用）

评测/压测
  bench [cmd]   评测与压测统一入口（bash scripts/dev.sh bench help）

小程序
  miniapp       npm 依赖（已装则秒级；缺 node_modules 时按 lock 精确装）

提示
  psql / redis 是交互式命令：mintty 里自动套 winpty；没有 TTY（管道、CI）时直接报错退出。

其它
  help          显示本帮助
EOF
}

die() { echo "✗ $*" >&2; exit 1; }
# 守护进程没起来时，compose 的报错是「cannot connect to the docker API at npipe://...」，
# 混在拉镜像日志里看不出是「没启动 Docker Desktop」；先用 docker info 一次问清楚。
need_compose() {
  command -v docker >/dev/null 2>&1 || die "没找到 docker，先装 Docker Desktop"
  docker info >/dev/null 2>&1 || die "docker 守护进程没起来：启动 Docker Desktop，等鲸鱼图标变绿再试"
}
need_venv() {
  [[ -x "$VENV_PY" ]] || die "缺少 .venv：先跑 bash scripts/dev.sh setup"
}

# 交互式容器命令（psql / redis）不带 -T，docker 必须有真 TTY。两种情况会失败：
#   1. 被管道/工具调用，本来就没有 TTY → 提前报错，别等 docker 甩 "the input device is not a TTY"
#   2. Git Bash 的 mintty 窗口：bash 自己有 pty（[[ -t 0 ]] 为真），但 docker.exe 是原生程序，
#      拿到的是管道 → 必须用 winpty 包一层。winpty 随 Git for Windows 一起装。
WINPTY=""
need_tty() {
  [[ -t 0 && -t 1 ]] || die "$1 是交互式命令，需要真终端（当前 stdin/stdout 是管道）：换 Git Bash 或 pwsh 直接跑"
  case "${TERM_PROGRAM:-}:$(tty 2>/dev/null)" in
    mintty:*|*:/dev/pty*)
      command -v winpty >/dev/null 2>&1 ||
        die "mintty 里 docker 拿不到控制台：装 Git for Windows 自带的 winpty，或在真实终端窗口里直接跑"
      WINPTY="winpty"
      echo "→ mintty 没有 Windows 控制台，已用 winpty 包装 docker exec" >&2
      ;;
  esac
}

COMPOSE_FILE="$ROOT/deploy/compose/dev.yml"
COMPOSE=(docker compose -f "$COMPOSE_FILE")

# 首次 up 的耗时几乎全在拉镜像（pgvector ~600MB）。compose 的 up 是「串行准备服务」，
# 拉取进度混在创建日志里、断了要重来；先 pull 一遍能把两件事解耦，也便于失败重试。
# 只拉本地缺的：镜像已在时跳过整轮 registry 往返（否则每次 up 多 1-2s 网络等待）。
compose_missing_images() {
  local img
  "${COMPOSE[@]}" config --images 2>/dev/null | sort -u | while read -r img; do
    [ -n "$img" ] || continue
    docker image inspect "$img" >/dev/null 2>&1 || echo "$img"
  done
}

cmd_up() {
  need_compose
  local missing
  missing="$(compose_missing_images)"
  if [ -n "$missing" ]; then
    echo "→ 本地缺镜像，先并行拉取：$(echo "$missing" | tr '\n' ' ')"
    # 拉取失败（镜像仓库被墙/限速）不阻塞：已有镜像照样能起
    "${COMPOSE[@]}" pull --ignore-pull-failures || echo "! 部分镜像拉取失败，继续用本地镜像" >&2
  fi
  # --wait：等 healthcheck 变 healthy 再返回，替代「起完手动 ps 反复看」
  "${COMPOSE[@]}" up -d --wait --wait-timeout 180
  cmd_ps
}
cmd_down() { need_compose; docker compose -f "$COMPOSE_FILE" down; }
cmd_ps() { need_compose; docker compose -f "$COMPOSE_FILE" ps; }
cmd_logs() { need_compose; docker compose -f "$COMPOSE_FILE" logs -f; }
cmd_psql() { need_compose; need_tty psql; ${WINPTY:+winpty} docker compose -f "$COMPOSE_FILE" exec postgres psql -U postgres -d "$DB_NAME"; }
cmd_redis() { need_compose; need_tty redis; ${WINPTY:+winpty} docker compose -f "$COMPOSE_FILE" exec redis redis-cli; }

cmd_dev() {
  need_venv
  [[ -f "$ENV_FILE" ]] || die "缺少 $ENV_FILE（被 git 忽略），没有它连不上库与 Redis"
  set -a; . "$ENV_FILE"; set +a
  cd "$ROOT"
  exec "$VENV_PY" -m uvicorn gateway.main:app \
    --host 0.0.0.0 --port "${PORT:-8080}" \
    --reload --reload-dir "$SRC_DIR"
}

cmd_serve() {
  need_venv
  [[ -f "$ENV_FILE" ]] || die "缺少 $ENV_FILE（被 git 忽略）"
  set -a; . "$ENV_FILE"; set +a
  cd "$ROOT"
  exec "$VENV_PY" -m uvicorn gateway.main:app \
    --host 0.0.0.0 --port "${PORT:-8080}" --no-proxy-headers
}

cmd_test() { need_venv; (cd "$ROOT" && "$VENV_PY" tests/contract/smoke_gateway.py); }
cmd_lint() { need_venv; (cd "$ROOT" && "$VENV_PY" -m ruff check src tests scripts); }
cmd_fmt() { need_venv; (cd "$ROOT" && "$VENV_PY" -m ruff format src tests scripts); }

cmd_migrate() {
  need_compose
  for f in "$ROOT"/sql/migrations/*.sql; do
    echo "→ $(basename "$f")"
    docker compose -f "$COMPOSE_FILE" exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d "$DB_NAME" < "$f" >/dev/null
  done
  echo "✓ migrations applied"
}

# npm install 的耗时几乎全在两件跟依赖树无关的事：向 registry 查漏洞库 + 募捐提示，
# 各一次网络往返。实测同一 node_modules 上 4.0s → 0.8s，与「1311 个文件」无关。
# node_modules 不在时用 npm ci：按 package-lock 精确重装（2.1s），不重新解析版本。
cmd_miniapp() {
  cd "$MINIAPP_DIR"
  if [[ -d node_modules ]]; then
    npm install --no-audit --no-fund
  else
    npm ci --no-audit --no-fund
  fi
}

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
    echo "→ uv pip install -r requirements/dev.txt"
    uv pip install --python "$VENV_PY" -r "$DEV_REQS" || die "uv pip install 失败"
  else
    echo "→ pip install（分钟级）"
    # uv 建的 venv 默认不带 pip；ensurepip 只补引导程序，不下载任何第三方包
    "$VENV_PY" -m pip --version >/dev/null 2>&1 || "$VENV_PY" -m ensurepip --upgrade >/dev/null
    "$VENV_PY" -m pip install -r "$DEV_REQS" || die "pip install 失败"
  fi
  echo "✓ 依赖就绪（Python $(venv_ver)）；下一步：bash scripts/dev.sh dev"
}

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
  help|-h|--help) usage ;;
  *)              echo "✗ 未知命令：$cmd" >&2; echo; usage; exit 1 ;;
esac
