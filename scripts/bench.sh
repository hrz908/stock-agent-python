#!/usr/bin/env bash
# 溜来（stock-agent）评测 / 压测统一入口
#
# 环境：Git Bash / MSYS2（Windows）或任意 bash
# 用法：bash scripts/bench.sh <命令>      # 不用 chmod 也能跑
#      bash scripts/bench.sh help        # 看全部命令
#
# 口径（重要）：
# - 默认全部走生产链路 http://127.0.0.1:8080/api/v1/agent/stream（JWT + SSE）；
#   只有显式 `inproc` 才进程内跑图——那条路径不过网关/限流，延迟不能对外。
# - 合成 uid 用 --users 摊开单用户限流（agent.qps=2 / concurrent=3）。
# - 4C4G 机器上 harness 与业务容器分开跑，且不要在生产容器里跑 inproc。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY="$ROOT/.venv/Scripts/python.exe"

PY=("$VENV_PY" -X utf8)
BENCH_DIR="$ROOT/tests/benchmark"
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
TAG="${TAG:-latest}"
CONC="${CONC:-4}"
USERS="${USERS:-}"          # 空 = 等于并发
DURATION="${DURATION:-45}"
STEPS="${STEPS:-1,4,8,16}"
SMOKE_IDS="${SMOKE_IDS:-Q-01,F-01,T-01,C-01}"

usage() {
  cat <<'EOF'
溜来 · 评测与压测（bash scripts/bench.sh <命令>）

质量
  smoke        4 条冒烟（生产 http，并发 2）        约 1 分钟
  quality      全量题库 + 规则断言 + 报告            约 10-15 分钟
  judge        quality 之后追加 LLM 评委（默认抽样 30%，JUDGE_SAMPLE 可调）
  retrieval    检索集 N×k 扫描（需先有 retrieval_cases.jsonl）
  inproc       进程内跑图，仅用于改 prompt 后的快速调试（数字不作性能）

性能
  perf         阶梯压测 + 资源采样                  约 4×duration
  perf-quick   1/4 两档 ×20s 的快速回归

结果
  report       重新渲染质量报告        TAG=xxx
  compare      质量 A/B 对比           BEFORE=xxx AFTER=yyy
  help

常用环境变量：
  BASE_URL=http://127.0.0.1:8080   CONC=4   USERS=4   TAG=baseline
  DURATION=45   STEPS=1,4,8,16   CONTAINER=stock-agent   SAMPLE_PG=1
  JUDGE_IDS=Q-01,Q-02 (只评这些 id)   JUDGE_SAMPLE=0.3 (评委抽样比例)

长任务放后台（quality / judge / perf 十几分钟起，前台会一直占着终端）：
  nohup bash scripts/bench.sh quality TAG=nightly </dev/null >>.bench.log 2>&1 &
  echo $! > .bench.pid
  tail -n 50 .bench.log     # 看进度；Ctrl+C 只退出 tail，不影响任务
  kill "$(cat .bench.pid)"  # 需要中止时
注意 </dev/null 不能省：只重定向 stdout 时，stdin 的管道会让调用方一直等到任务结束。
EOF
}

die() { echo "✗ $*" >&2; exit 1; }
need_venv() {
  [[ -x "$VENV_PY" ]] || die "缺少 .venv：python -m venv .venv && uv pip install -r requirements/dev.txt"
}

load_env() {
  # 与 scripts/dev.sh 同口径：.env 提供 JWT_SECRET / DSN（后者由 harness 映射为 DATABASE_URL）
  if [[ -f "$ROOT/.env" ]]; then
    set -a; . "$ROOT/.env"; set +a
  fi
}

cmd_smoke() {
  need_venv; load_env
  (cd "$ROOT" && "${PY[@]}" tests/benchmark/run.py run \
    --tag smoke --mode http --base-url "$BASE_URL" \
    --concurrency 2 --users 2 --ids "$SMOKE_IDS")
}

cmd_quality() {
  need_venv; load_env
  local -a uargs=()
  [[ -n "$USERS" ]] && uargs=(--users "$USERS")
  cd "$ROOT"
  "${PY[@]}" tests/benchmark/run.py run \
    --tag "$TAG" --mode http --base-url "$BASE_URL" \
    --concurrency "$CONC" "${uargs[@]}"
  # 规则断言是必跑项；评委按 JUDGE_IDS 抽样（空则跳过，见 judge 命令）
  "${PY[@]}" tests/benchmark/run.py eval --tag "$TAG" --no-judge
  "${PY[@]}" tests/benchmark/run.py report --tag "$TAG"
}

cmd_judge() {
  need_venv; load_env
  local -a jargs=()
  [[ -n "${JUDGE_IDS:-}" ]] && jargs=(--ids "$JUDGE_IDS")
  cd "$ROOT"
  "${PY[@]}" tests/benchmark/run.py eval --tag "$TAG" \
    --judge-sample "${JUDGE_SAMPLE:-0.3}" "${jargs[@]}"
  "${PY[@]}" tests/benchmark/run.py report --tag "$TAG"
}

cmd_retrieval() {
  need_venv; load_env
  [[ -f "$BENCH_DIR/retrieval_cases.jsonl" ]] || die \
    "缺少 $BENCH_DIR/retrieval_cases.jsonl；先跑 gen_retrieval_cases.py 生成评测集"
  (cd "$ROOT" && "${PY[@]}" tests/benchmark/sweep_retrieval.py all \
    --tag "${RETRIEVAL_TAG:-retrieval}")
}

cmd_inproc() {
  need_venv; load_env
  (cd "$ROOT" && "${PY[@]}" tests/benchmark/run.py run \
    --tag "${TAG}-inproc" --mode inproc --concurrency 2)
}

cmd_perf() {
  need_venv; load_env
  local -a res_args=()
  [[ -n "${CONTAINER:-}" ]] && res_args+=(--container "$CONTAINER")
  [[ -n "${SAMPLE_PG:-}" ]] && res_args+=(--sample-pg)
  (cd "$ROOT" && "${PY[@]}" tests/benchmark/perf.py \
    --tag "$TAG" --base-url "$BASE_URL" --steps "$STEPS" \
    --duration "$DURATION" "${res_args[@]}")
}

cmd_perf_quick() {
  need_venv; load_env
  (cd "$ROOT" && "${PY[@]}" tests/benchmark/perf.py \
    --tag "${TAG}-quick" --base-url "$BASE_URL" \
    --steps "1,4" --duration 20 --cooldown 3)
}

cmd_report() {
  need_venv; load_env
  (cd "$ROOT" && "${PY[@]}" tests/benchmark/run.py report --tag "$TAG")
}

cmd_compare() {
  need_venv; load_env
  [[ -n "${BEFORE:-}" && -n "${AFTER:-}" ]] || die "compare 需要 BEFORE=xxx AFTER=yyy"
  (cd "$ROOT" && "${PY[@]}" tests/benchmark/run.py compare \
    --before "$BEFORE" --after "$AFTER")
}

cmd="${1:-help}"
case "$cmd" in
  smoke)        cmd_smoke ;;
  quality)      cmd_quality ;;
  judge)        cmd_judge ;;
  retrieval)    cmd_retrieval ;;
  inproc)       cmd_inproc ;;
  perf)         cmd_perf ;;
  perf-quick)   cmd_perf_quick ;;
  report)       cmd_report ;;
  compare)      cmd_compare ;;
  help|-h|--help) usage ;;
  *)            echo "✗ 未知命令：$cmd" >&2; echo; usage; exit 1 ;;
esac
