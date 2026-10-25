"""报告生成：单次测试报告 + A/B 对比报告（markdown）。"""
from datetime import datetime
from pathlib import Path

from .cases import CATEGORY_NAMES, DIFFICULTY_NAMES

DIM_NAMES = {
    "accuracy": "准确性", "completeness": "完整性", "relevance": "相关性",
    "timeliness": "时效性", "clarity": "表达清晰度",
}


def _fmt(v, suffix=""):
    return "—" if v is None else f"{v}{suffix}"


def render_report(results: list, evals: list, summary: dict, tag: str, mode: str) -> str:
    lines = []
    add = lines.append
    add(f"# 股票问答 Agent 测试报告（{tag}）\n")
    add(f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    add(f"- 模式：{mode}｜用例数：{summary['cases']}")
    total_s = sum(r.get("elapsed_ms_total", 0) for r in results) / 1000
    add(f"- 总耗时：{total_s:.0f}s\n")

    add("## 总览\n")
    add(f"- 规则通过率：**{_fmt(summary['rule_pass_rate'], '')}**"
        f"（{summary['cases_all_rules_pass']}/{summary['cases']} 用例全部通过）")
    add(f"- LLM 评委均分：**{_fmt(summary['judge_avg'])} / 5**\n")
    add("| 维度 | 均分 |")
    add("| --- | --- |")
    for k, name in DIM_NAMES.items():
        add(f"| {name} | {_fmt(summary['dims'].get(k))} |")
    add("")

    add("## 分类表现\n")
    add("| 类型 | 用例数 | 规则通过率 | 评委均分 |")
    add("| --- | --- | --- | --- |")
    for k, v in summary["by_category"].items():
        add(f"| {CATEGORY_NAMES.get(k, k)} | {v['cases']} | {_fmt(v['rule_pass_rate'])} "
            f"| {_fmt(v['judge_avg'])} |")
    add("")
    add("| 难度 | 用例数 | 规则通过率 | 评委均分 |")
    add("| --- | --- | --- | --- |")
    for k, v in summary["by_difficulty"].items():
        add(f"| {DIFFICULTY_NAMES.get(k, k)} | {v['cases']} | {_fmt(v['rule_pass_rate'])} "
            f"| {_fmt(v['judge_avg'])} |")
    add("")

    if summary.get("failure_clusters"):
        add("## 失败聚类（按规则断言）\n")
        for check, ids in summary["failure_clusters"].items():
            add(f"- **{check}** × {len(ids)}：{', '.join(ids)}")
        add("")

    if summary.get("worst"):
        add("## 重点问题用例\n")
        for w in summary["worst"]:
            if not w["failures"] and (w["judge_avg"] or 5) >= 4:
                continue
            add(f"### {w['id']}（{CATEGORY_NAMES.get(w['category'], w['category'])}）"
                f"规则失败 {w['rule_fail']} 项，评委 {_fmt(w['judge_avg'])}")
            if w["failures"]:
                add(f"- 规则失败：{', '.join(w['failures'])}")
            for issue in w["issues"]:
                add(f"- 问题：{issue}")
            add("")

    add("## 逐用例明细\n")
    by_id = {r["id"]: r for r in results}
    for ev in evals:
        r = by_id.get(ev["id"], {})
        turns = r.get("turns", [])
        add(f"### {ev['id']} [{CATEGORY_NAMES.get(ev['category'], ev['category'])}/"
            f"{DIFFICULTY_NAMES.get(ev['difficulty'], ev['difficulty'])}]"
            f" 规则 {ev['rule_passed']}/{ev['rule_total']}，评委 {_fmt(ev['judge_avg'])}\n")
        add(f"- 设计意图：{ev['rationale']}")
        add(f"- 路由：{r.get('final', {}).get('intent', '?')}｜工具："
            f"{', '.join(r.get('all_tools', [])) or '无'}｜卡片："
            f"{', '.join(r.get('all_cards', [])) or '无'}")
        for f in ev["rule_failures"]:
            add(f"- ❌ {f['check']}：{f['detail']}")
        j = ev["judge"]
        if isinstance(j, dict) and j.get("accuracy") is not None:
            add(f"- 评委：准确 {j['accuracy']}｜完整 {j['completeness']}｜相关 "
                f"{j['relevance']}｜时效 {j['timeliness']}｜清晰 {j['clarity']}")
        for issue in (j.get("issues") or [])[:5]:
            add(f"- 问题：{issue}")
        if j.get("suggestion"):
            add(f"- 建议：{j['suggestion']}")
        if turns:
            text = (turns[-1].get("text") or "").strip()
            preview = text if len(text) <= 600 else text[:600] + " …"
            add(f"\n<details><summary>最终回答（{len(text)} 字）</summary>\n\n{preview}\n\n</details>\n")
    return "\n".join(lines)


def render_compare(before: dict, after: dict, tag_b: str, tag_a: str,
                   evals_b: list, evals_a: list) -> str:
    """A/B 对比：总体指标 + 分维度 + 分类型 + 逐用例变化。"""
    lines = []
    add = lines.append
    add(f"# A/B 对比：{tag_b} → {tag_a}\n")
    add("| 指标 | 前 | 后 | 变化 |")
    add("| --- | --- | --- | --- |")

    def row(name, b, a, higher_better=True, pct=False):
        if b is None or a is None:
            add(f"| {name} | {_fmt(b)} | {_fmt(a)} | — |")
            return
        delta = a - b
        sign = "+" if delta > 0 else ""
        good = (delta > 0) if higher_better else (delta < 0)
        mark = " ✅" if abs(delta) > 1e-9 and good else (" ⚠️" if abs(delta) > 1e-9 else "")
        fmt = (lambda x: f"{x:.1%}") if pct else (lambda x: f"{x:.2f}")
        add(f"| {name} | {fmt(b)} | {fmt(a)} | {sign}{fmt(delta).lstrip('+')}{mark} |")

    row("规则通过率", before["rule_pass_rate"], after["rule_pass_rate"], pct=True)
    row("全部通过的用例数", before["cases_all_rules_pass"], after["cases_all_rules_pass"])
    row("评委均分", before["judge_avg"], after["judge_avg"])
    for k, name in DIM_NAMES.items():
        row(name, before["dims"].get(k), after["dims"].get(k))
    add("")

    add("## 分类型对比\n")
    add("| 类型 | 规则通过率 前→后 | 评委均分 前→后 |")
    add("| --- | --- | --- |")
    cats = sorted(set(before["by_category"]) | set(after["by_category"]))
    for c in cats:
        b = before["by_category"].get(c, {})
        a = after["by_category"].get(c, {})
        add(f"| {CATEGORY_NAMES.get(c, c)} | {_fmt(b.get('rule_pass_rate'))} → "
            f"{_fmt(a.get('rule_pass_rate'))} | {_fmt(b.get('judge_avg'))} → "
            f"{_fmt(a.get('judge_avg'))} |")
    add("")

    # 逐用例：规则失败数 + 评委分变化
    b_by = {e["id"]: e for e in evals_b}
    a_by = {e["id"]: e for e in evals_a}
    add("## 逐用例变化\n")
    add("| 用例 | 规则失败 前→后 | 评委分 前→后 | 备注 |")
    add("| --- | --- | --- | --- |")
    for cid in sorted(set(b_by) | set(a_by)):
        b, a = b_by.get(cid), a_by.get(cid)
        if not b or not a:
            add(f"| {cid} | — | — | 仅单侧存在 |")
            continue
        bf = b["rule_total"] - b["rule_passed"]
        af = a["rule_total"] - a["rule_passed"]
        note = ""
        if af < bf:
            note = "修复"
        elif af > bf:
            note = "**回退**"
        elif (a["judge_avg"] or 0) > (b["judge_avg"] or 0) + 0.3:
            note = "提升"
        elif (a["judge_avg"] or 0) < (b["judge_avg"] or 0) - 0.3:
            note = "**下降**"
        add(f"| {cid} | {bf} → {af} | {_fmt(b['judge_avg'])} → {_fmt(a['judge_avg'])} | {note} |")
    add("")
    return "\n".join(lines)


def save_report(md: str, path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(md, encoding="utf-8")
