#!/usr/bin/env python
"""股票问答 Agent 测试与调优流水线 CLI。

用法（Windows 控制台务必带 -X utf8，否则中文输出触发 cp1252 报错）：

  # 1) 跑一批用例（默认 in-process 模式，直接跑工作区代码）
  python -X utf8 tests/benchmark/run.py run --tag baseline

  # 2) 评估（规则 + LLM 评委）
  python -X utf8 tests/benchmark/run.py eval --tag baseline

  # 3) 生成报告
  python -X utf8 tests/benchmark/run.py report --tag baseline

  # 4) A/B 对比（调优前后）
  python -X utf8 tests/benchmark/run.py compare --before baseline --after tuned

  # 一条龙：run + eval + report
  python -X utf8 tests/benchmark/run.py all --tag tuned

过滤：--ids Q-01,F-02｜--category concept｜--difficulty hard
HTTP 模式（打运行中的服务）：--mode http --base-url http://localhost:8000
只跑规则不调评委：eval --no-judge
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Windows：psycopg 异步驱动不能用 ProactorEventLoop，必须换成 Selector 变体
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # agent/ 进 sys.path（与 tests/test_regression.py 一致）
sys.path.insert(0, str(HERE.parents[0]))  # agent/tests/ 进 sys.path（benchmark 包所在）

from benchmark import evaluate as ev  # noqa: E402
from benchmark import report as rp  # noqa: E402
from benchmark import runner  # noqa: E402
from benchmark.cases import CASES, select_cases  # noqa: E402

RUNS_DIR = HERE / "runs"


def _progress(msg):
    print(msg, flush=True)


async def _cmd_run(args) -> Path:
    cases = select_cases(ids=args.ids, category=args.category, difficulty=args.difficulty)
    if not cases:
        print("没有匹配的用例")
        return RUNS_DIR / args.tag
    if args.mode == "inproc":
        runner.apply_inproc_env(verbose=True)
    from datetime import datetime
    run_id = f"{args.tag}-{datetime.now().strftime('%H%M%S')}"
    results = await runner.run_batch(
        cases, mode=args.mode, run_id=run_id, concurrency=args.concurrency,
        base_url=args.base_url, progress=_progress)
    out = RUNS_DIR / args.tag
    runner.save_results(results, out / "results.jsonl")
    print(f"DONE -> {out / 'results.jsonl'} ({len(results)} cases)")
    return out


async def _cmd_eval(args) -> Path:
    # 评委要调 LLM：即使结果是 http 模式产生的，本进程也需要 env
    runner.apply_inproc_env()
    results = runner.load_results(RUNS_DIR / args.tag / "results.jsonl")
    by_id = {c.id: c for c in CASES}
    evals = await ev.evaluate(results, by_id, with_judge=not getattr(args, "no_judge", False),
                              concurrency=args.concurrency, progress=_progress)
    summary = ev.summarize(evals)
    out = RUNS_DIR / args.tag
    ev.save_evals(evals, out / "evals.json")
    (out / "summary.json").write_text(
        __import__("json").dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"rule_pass_rate={summary['rule_pass_rate']} judge_avg={summary['judge_avg']}")
    print(f"DONE -> {out / 'evals.json'}")
    return out


def _cmd_report(args) -> Path:
    results = runner.load_results(RUNS_DIR / args.tag / "results.jsonl")
    evals = ev.load_evals(RUNS_DIR / args.tag / "evals.json")
    summary = ev.summarize(evals)
    mode = results[0].get("mode", "inproc") if results else "inproc"
    md = rp.render_report(results, evals, summary, args.tag, mode)
    out = RUNS_DIR / args.tag / "report.md"
    rp.save_report(md, out)
    print(f"DONE -> {out}")
    return out


def _cmd_compare(args) -> Path:
    import json
    sum_b = json.loads((RUNS_DIR / args.before / "summary.json").read_text(encoding="utf-8"))
    sum_a = json.loads((RUNS_DIR / args.after / "summary.json").read_text(encoding="utf-8"))
    evals_b = ev.load_evals(RUNS_DIR / args.before / "evals.json")
    evals_a = ev.load_evals(RUNS_DIR / args.after / "evals.json")
    md = rp.render_compare(sum_b, sum_a, args.before, args.after, evals_b, evals_a)
    out = RUNS_DIR / f"compare_{args.before}_vs_{args.after}.md"
    rp.save_report(md, out)
    print(md[:2000])
    print(f"DONE -> {out}")
    return out


async def _cmd_all(args) -> Path:
    await _cmd_run(args)
    await _cmd_eval(args)
    return _cmd_report(args)


def main():
    ap = argparse.ArgumentParser(description="股票问答 Agent 测试与调优流水线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--tag", required=True, help="运行标签（结果目录名）")
        p.add_argument("--mode", default="inproc", choices=["inproc", "http"])
        p.add_argument("--base-url", default="http://localhost:8000")
        p.add_argument("--concurrency", type=int, default=4)
        p.add_argument("--ids", default=None, help="逗号分隔的用例 id")
        p.add_argument("--category", default=None)
        p.add_argument("--difficulty", default=None)

    p_run = sub.add_parser("run", help="批量执行用例")
    common(p_run)
    p_eval = sub.add_parser("eval", help="评估结果")
    common(p_eval)
    p_eval.add_argument("--no-judge", action="store_true", help="只跑规则断言")
    p_rep = sub.add_parser("report", help="生成报告")
    common(p_rep)
    p_cmp = sub.add_parser("compare", help="A/B 对比")
    p_cmp.add_argument("--before", required=True)
    p_cmp.add_argument("--after", required=True)
    p_all = sub.add_parser("all", help="run + eval + report 一条龙")
    common(p_all)

    args = ap.parse_args()
    if getattr(args, "ids", None):
        args.ids = [s.strip() for s in args.ids.split(",") if s.strip()]

    if args.cmd == "run":
        asyncio.run(_cmd_run(args))
    elif args.cmd == "eval":
        asyncio.run(_cmd_eval(args))
    elif args.cmd == "report":
        _cmd_report(args)
    elif args.cmd == "compare":
        _cmd_compare(args)
    elif args.cmd == "all":
        asyncio.run(_cmd_all(args))


if __name__ == "__main__":
    main()
