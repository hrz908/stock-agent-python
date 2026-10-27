#!/usr/bin/env python
"""阶梯压测：并发 → 吞吐/尾延迟曲线，并采资源与限流/降级计数。

与 `run.py` 的分工：
- run.py   : 按题库逐条跑，回答“答得对不对”（质量）；
- perf.py  : 固定问句反复打生产链路，回答“扛不扛得住”（容量）。

关键口径：
- 必须打 `/api/v1/agent/stream`（JWT + SSE），inproc 模式不过网关、无限流，不能测容量；
- 每个虚拟用户一个合成 uid：限流按 uid 分片（qps=2 / concurrent=3），单 uid 打不出并发；
- 判定饱和看三件事：P95 对比低并发是否翻倍、错误率是否超阈值、容器 RSS 是否逼近限额；
- 重排失败会静默回退 RRF（延迟变好、质量变差），所以单列 429 与错误，不允许只看 RPS。

用法（Windows 控制台务必带 -X utf8）：
  python -X utf8 tests/benchmark/perf.py --tag perf1
  python -X utf8 tests/benchmark/perf.py --tag perf1 --steps 1,4,8,16 --duration 45
  python -X utf8 tests/benchmark/perf.py --tag perf1 --container stock-agent
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))  # src/：gateway/agent/ingest 包根
sys.path.insert(0, str(HERE.parents[1]))  # tests/

from benchmark import sse  # noqa: E402
from benchmark.cases import CASES  # noqa: E402

RUNS_DIR = HERE / "runs"
DEFAULT_IDS = "Q-01,F-01,T-01,C-01,K-01,R-01"
DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/stock_agent"


# ===== 统计小工具 =====

def _pct(values: list, p: float):
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
    return s[idx]


def _mean(values: list):
    return round(sum(values) / len(values), 1) if values else None


def _r(value, digits: int = 0):
    return "—" if value is None else f"{value:.{digits}f}" if digits else str(int(value))


def _safe(name: str, limit: int = 40) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "-", name)[:limit]


# ===== 单请求 =====

async def _one_request(client, base_url: str, token: str, query: str, session_id: str) -> dict:
    t0 = time.monotonic()
    rec = {
        "query": query, "ok": False, "error": "", "http_status": 200,
        "ttft_ms": None, "first_event_ms": None, "total_ms": None, "events": 0,
    }
    try:
        async for ev, payload in sse.agent_stream(client, base_url, token, query, session_id):
            data = payload if isinstance(payload, dict) else {}
            rec["events"] += 1
            now = int((time.monotonic() - t0) * 1000)
            if rec["first_event_ms"] is None:
                rec["first_event_ms"] = now
            if ev in {"token", "card", "table"} and rec["ttft_ms"] is None:
                rec["ttft_ms"] = now
            if ev == "done":
                rec["ok"] = True
            elif ev == "error":
                rec["error"] = str(data.get("message") or data.get("msg")
                                   or data.get("code") or "error")[:200]
    except sse.StreamHttpError as exc:
        rec["http_status"] = exc.status_code
        rec["error"] = f"http_{exc.status_code}"
    except Exception as exc:  # noqa: BLE001 - 单请求失败只计数，不中断阶梯
        rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
    rec["total_ms"] = int((time.monotonic() - t0) * 1000)
    if rec["error"]:
        rec["ok"] = False
    return rec


async def _run_step(
    queries: list,
    workers: int,
    duration: float,
    base_url: str,
    secret: str,
    uid_base: int,
    tag: str,
    step_no: int,
) -> list:
    """workers 个虚拟用户各跑一条闭环，持续 duration 秒。"""
    import httpx

    deadline = time.monotonic() + duration
    records: list = []
    lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=max(8, workers * 2),
                          max_keepalive_connections=max(4, workers))
    timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=30.0)

    async with httpx.AsyncClient(timeout=timeout, limits=limits, trust_env=False) as client:
        async def worker(wid: int):
            uid = uid_base + wid
            token = sse.sign_token(uid, secret)
            # tag 先限长，保证 step/wid 不会被 64 字符上限截掉（否则并发档位会共用一个会话）
            session_id = _safe(f"perf-{tag[:20]}-{step_no}-{wid}", 64)
            i = 0
            while time.monotonic() < deadline:
                query = queries[i % len(queries)]
                i += 1
                rec = await _one_request(client, base_url, token, query, session_id)
                async with lock:
                    records.append(rec)

        await asyncio.gather(*[worker(i) for i in range(workers)])
    return records


# ===== 资源采样 =====

def _docker_stats(container: str) -> dict:
    """一次 docker stats 采样；失败返回空，不影响压测。"""
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.MemUsage}}|{{.CPUPerc}}", container],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return {}
    usage, _, cpu = out.partition("|")
    mem_mb = _parse_mem(usage)
    try:
        cpu_pct = float(cpu.strip().rstrip("%"))
    except ValueError:
        cpu_pct = None
    return {"mem_mb": mem_mb, "cpu_pct": cpu_pct}


def _parse_mem(text: str):
    """`512.3MiB / 1.367GiB` → 512.3（单位 MiB）。"""
    head = text.split("/")[0].strip()
    match = re.match(r"([0-9.]+)\s*([KMGT]?i?B)", head, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    factor = {"B": 1 / 1024 / 1024, "KB": 1 / 1024, "KIB": 1 / 1024,
              "MB": 1, "MIB": 1, "GB": 1024, "GIB": 1024}.get(unit, 1)
    return round(value * factor, 1)


def _pg_active(dsn: str):
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND state = 'active'"
            ).fetchone()
        return int(row[0]) if row else None
    except Exception:  # noqa: BLE001 - 观测失败不阻断压测
        return None


def _audit_step(dsn: str, uids: list, since) -> dict:
    """从 agent_runs 统计本轮的重排降级次数。

    重排 429/超时后会静默回退 RRF：延迟变好、质量变差，客户端完全看不出来，
    只能读 timings.rerank_failed。这条指标缺失时，压测结论会把降级当成性能提升。
    """
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT count(*), "
                "count(*) FILTER (WHERE coalesce((timings->>'rerank_failed')::int, 0) > 0) "
                "FROM agent_runs WHERE user_id = ANY(%s) AND created_at >= %s",
                (uids, since),
            ).fetchone()
        return {"audit_runs": int(row[0] or 0), "rerank_failed_runs": int(row[1] or 0)}
    except Exception:  # noqa: BLE001 - 观测失败不阻断压测
        return {}


async def _sample_resources(stop: asyncio.Event, container: str | None, dsn: str | None,
                            interval: float = 5.0) -> list:
    samples = []
    while not stop.is_set():
        row: dict = {"ts": time.time()}
        if container:
            row.update(await asyncio.to_thread(_docker_stats, container))
        if dsn:
            row["pg_active"] = await asyncio.to_thread(_pg_active, dsn)
        if len(row) > 1:
            samples.append(row)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
    return samples


# ===== 汇总 =====

def _summarize(step: int, records: list, resources: list, elapsed_s: float,
               audit: dict | None = None) -> dict:
    ok = [r for r in records if r["ok"]]
    ttft = [r["ttft_ms"] for r in ok if r["ttft_ms"] is not None]
    total = [r["total_ms"] for r in records if r["total_ms"] is not None]
    errors: dict = {}
    for r in records:
        if r["error"]:
            key = r["error"].split(":")[0][:40]
            errors[key] = errors.get(key, 0) + 1
    elapsed = max(elapsed_s, 0.001)
    mem = [s["mem_mb"] for s in resources if s.get("mem_mb") is not None]
    cpu = [s["cpu_pct"] for s in resources if s.get("cpu_pct") is not None]
    pg = [s["pg_active"] for s in resources if s.get("pg_active") is not None]
    return {
        "users": step,
        "requests": len(records),
        "ok": len(ok),
        "success_rate": round(len(ok) / len(records), 3) if records else 0.0,
        "rps": round(len(records) / elapsed, 2) if elapsed else 0.0,
        "ttft_p50": _pct(ttft, 50), "ttft_p90": _pct(ttft, 90),
        "ttft_p95": _pct(ttft, 95), "ttft_p99": _pct(ttft, 99),
        "total_p50": _pct(total, 50), "total_p95": _pct(total, 95),
        "total_p99": _pct(total, 99),
        "http_429": sum(1 for r in records if r["http_status"] == 429),
        "errors": errors,
        "mem_peak_mb": max(mem) if mem else None,
        "mem_avg_mb": _mean(mem),
        "cpu_avg_pct": _mean(cpu),
        "cpu_peak_pct": max(cpu) if cpu else None,
        "pg_active_avg": _mean(pg),
        "pg_active_peak": max(pg) if pg else None,
        "audit_runs": (audit or {}).get("audit_runs"),
        "rerank_failed_runs": (audit or {}).get("rerank_failed_runs"),
    }


def _render(summaries: list, tag: str, duration: float) -> str:
    lines = [f"# 阶梯压测报告（{tag}）\n",
             f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"- 每档时长：{duration:.0f}s｜入口：`/api/v1/agent/stream`（JWT + SSE）\n",
             "| 并发 | 请求数 | 成功率 | RPS | TTFT P50 | P90 | P95 | P99 "
             "| 总耗时 P50 | P95 | P99 | 429 | 错误 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in summaries:
        lines.append(
            f"| {s['users']} | {s['requests']} | {s['success_rate']:.1%} | {s['rps']} "
            f"| {_r(s['ttft_p50'])} | {_r(s['ttft_p90'])} | {_r(s['ttft_p95'])} "
            f"| {_r(s['ttft_p99'])} | {_r(s['total_p50'])} | {_r(s['total_p95'])} "
            f"| {_r(s['total_p99'])} | {s['http_429']} | {len(s['errors'])} |"
        )
    lines.append("")
    lines.append("注：TTFT 首字为 token/card/table；429 为握手阶段被限流；"
                 "错误列是去重后的类型数，明细见 perf.json。\n")

    has_res = any(
        s["mem_peak_mb"] is not None or s["pg_active_avg"] is not None
        or s["audit_runs"] is not None
        for s in summaries
    )
    if has_res:
        lines += ["## 资源与降级\n",
                  "| 并发 | RSS 峰值(MB) | RSS 均值 | CPU 均值(%) | CPU 峰值(%) "
                  "| PG active 均值 | 峰值 | 审计行 | 重排降级(轮) |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for s in summaries:
            lines.append(
                f"| {s['users']} | {_r(s['mem_peak_mb'], 1)} | {_r(s['mem_avg_mb'], 1)} "
                f"| {_r(s['cpu_avg_pct'], 1)} | {_r(s['cpu_peak_pct'], 1)} "
                f"| {_r(s['pg_active_avg'], 1)} | {_r(s['pg_active_peak'])} "
                f"| {_r(s['audit_runs'])} | {_r(s['rerank_failed_runs'])} |"
            )
        lines.append("")
        lines.append("注：重排降级 = timings.rerank_failed>0 的轮数；该值上升时延迟会“变好”，"
                     "但检索质量已退化为 RRF，必须和规则通过率一起看。\n")

    order = sorted(summaries, key=lambda s: s["users"])
    base = order[0] if order else None
    if base and base["total_p95"]:
        knee = next(
            (s for s in order[1:] if (s["total_p95"] or 0) > base["total_p95"] * 2),
            None,
        )
        lines.append("## 结论\n")
        if knee:
            lines.append(
                f"- 拐点出现在并发 {knee['users']}：总耗时 P95 从 {base['total_p95']}ms "
                f"升到 {knee['total_p95']}ms（>2×）。"
            )
        else:
            lines.append("- 未出现 P95 翻倍的拐点：本档位内仍在线性区，可继续上调并发验证。")
        if any(s["http_429"] for s in order):
            lines.append("- 出现 429：确认压测 uid 数与 agent.qps / agent.concurrent 的关系，"
                         "限流态与无限流态的数字不能混用。")
        if any(s["errors"].get("http_500") for s in order):
            lines.append("- 出现 5xx：先看容器日志与 agent_runs.error_code，按无效运行处理。")
        lines.append("")
    return "\n".join(lines)


# ===== 入口 =====

def _queries(ids: str) -> list:
    wanted = [x.strip() for x in ids.split(",") if x.strip()]
    by_id = {c.id: c for c in CASES}
    picked = [by_id[i] for i in wanted if i in by_id]
    if not picked:
        raise SystemExit(f"没有匹配的用例：{ids}")
    return [c.turns[0] for c in picked if c.turns]


async def _main(args) -> int:
    secret = sse.jwt_secret(args.jwt_secret)
    queries = _queries(args.ids)
    steps = [int(x) for x in args.steps.split(",") if x.strip()]
    tag = args.tag
    out_dir = RUNS_DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[perf] {len(steps)} 档｜每档 {args.duration:.0f}s｜问句 {len(queries)} 条")
    summaries = []
    for step_no, workers in enumerate(steps):
        stop = asyncio.Event()
        sampler = asyncio.create_task(
            _sample_resources(stop, args.container, args.dsn if args.sample_pg else None)
        )
        started = time.monotonic()
        step_started_at = datetime.now().astimezone()
        records = await _run_step(
            queries, workers, args.duration, args.base_url, secret,
            args.user_id + step_no * 100, tag, step_no,
        )
        stop.set()
        resources = await sampler
        elapsed_s = time.monotonic() - started
        uids = [args.user_id + step_no * 100 + i for i in range(workers)]
        audit = (
            await asyncio.to_thread(_audit_step, args.dsn, uids, step_started_at)
            if args.sample_pg
            else None
        )
        summary = _summarize(workers, records, resources, elapsed_s, audit)
        summaries.append(summary)
        print(
            f"[perf] 并发 {workers:>2}: {summary['requests']} 请求 "
            f"{elapsed_s:.0f}s 成功率 {summary['success_rate']:.1%} "
            f"RPS {summary['rps']} TTFT P95 {_r(summary['ttft_p95'])}ms "
            f"总P95 {_r(summary['total_p95'])}ms 429={summary['http_429']}",
            flush=True,
        )
        if args.cooldown > 0 and workers != steps[-1]:
            await asyncio.sleep(args.cooldown)

    (out_dir / "perf.json").write_text(
        json.dumps({"tag": tag, "base_url": args.base_url, "steps": summaries},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    md = _render(summaries, tag, args.duration)
    (out_dir / "perf.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"DONE -> {out_dir / 'perf.md'}")

    bad = [s for s in summaries if s["success_rate"] < 1 - args.max_error_rate]
    if bad:
        print(f"✗ 错误率超阈值 {args.max_error_rate:.0%}："
              f"{[s['users'] for s in bad]}", flush=True)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="股票问答 Agent 阶梯压测")
    ap.add_argument("--tag", required=True, help="结果目录名")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--steps", default="1,4,8,16", help="并发档位，逗号分隔")
    ap.add_argument("--duration", type=float, default=45.0, help="每档持续秒数")
    ap.add_argument("--cooldown", type=float, default=5.0, help="档位间隔秒数")
    ap.add_argument("--ids", default=DEFAULT_IDS, help="问句用例 id，逗号分隔")
    ap.add_argument("--user-id", type=int, default=900001, help="起始合成 uid")
    ap.add_argument("--jwt-secret", default=None)
    ap.add_argument("--container", default=None, help="docker 容器名，用于采 RSS/CPU")
    ap.add_argument("--dsn", default=None, help="业务库 DSN（采 pg_stat_activity）")
    ap.add_argument("--sample-pg", action="store_true",
                    help="同时采 PG 活跃连接数与 agent_runs 重排降级次数")
    ap.add_argument("--max-error-rate", type=float, default=0.05,
                    help="超过该错误率以非 0 退出（默认 5%%）")
    args = ap.parse_args()

    from benchmark.runner import apply_env_file

    apply_env_file(verbose=True)
    args.dsn = args.dsn or os.environ.get("DATABASE_URL") or DEFAULT_DSN

    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
