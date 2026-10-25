"""质量评估：规则断言 + LLM 评委五维打分。

规则断言（确定性，不调 LLM）：
- 路由意图、工具调用、卡片发射、文案包含/排除、免责声明、非空、无 error

LLM 评委（get_small_llm 结构化输出）五个维度各 1~5 分：
- accuracy       准确性：数字与事实是否与工具返回一致，有无编造
- completeness   完整性：是否覆盖问题各部分
- relevance      相关性：是否切题，有无无关内容
- timeliness     时效性：是否使用当期数据并说明数据日期
- clarity        表达清晰度：结构、简洁、易懂
另输出 issues（定位到的错误与不足）与 suggestion（改进建议）。

总分 = 规则通过率 * 50 + 评委均分 / 5 * 50（加权前报告里分开展示）。
"""
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

_JUDGE_SYSTEM = """你是一个严格的股票问答质量评审员。你将看到用户问题、助手回答、以及助手背后工具返回的真实数据（如有）。
请按五个维度各打 1~5 的整数分，并列出发现的问题与改进建议。

评分口径：
- accuracy 准确性：回答中的数字、事实、涨跌方向必须与工具数据一致；无工具数据时的通识回答不得出现编造的具体数字。5=完全一致，3=有小错误，1=编造关键数字或事实性错误。
- completeness 完整性：是否回答了问题的每一个部分（如问 PE 和 PB 两个都答了才算完整）。5=全覆盖，3=漏一部分，1=基本没答。
- relevance 相关性：是否紧扣问题，有无跑题、无关标的、冗余铺陈。5=完全切题，3=少量冗余，1=跑题。
- timeliness 时效性：是否使用当期数据并体现数据时间（日期/报告期）；是否把过期数据当实时数据用。5=数据新且口径清晰，3=未说明数据时间，1=使用或暗示过期数据。
- clarity 表达清晰度：结构是否清晰、表述是否易懂、有无照搬 JSON 或术语堆砌。5=清晰专业，3=基本可读，1=混乱难读。

注意：没有工具数据的概念/闲聊类回答，accuracy 只评通识正确性，不得因为「没有实时数据」而扣分。
issues 用中文简述具体问题（没有则空数组）；suggestion 用中文给一条最关键的改进建议。
只输出结构化结果，不要额外解释。"""


def rule_check(case, result: dict) -> list:
    """对单个用例结果跑规则断言，返回 [{check, pass, detail}]。"""
    expect = case.expect
    final = result.get("final", {})
    text = (final.get("text") or "").strip()
    low = text.lower()
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    add("回答非空", bool(text), f"len={len(text)}")
    # guardrail_blocked 是护栏的预期审计码（用户仍会收到安全降级话术），不算失败
    real_errors = [e for e in result.get("errors", []) if "guardrail_blocked" not in e]
    add("无错误事件", not real_errors, "; ".join(real_errors)[:200])

    if "intent" in expect:
        got = final.get("intent", "")
        add("路由意图正确", got in expect["intent"], f"got={got!r} want={sorted(expect['intent'])}")

    if "tools_any" in expect:
        got = set(result.get("all_tools") or [])
        hit = got & set(expect["tools_any"])
        add("调用了预期工具", bool(hit), f"got={sorted(got)} want_any={expect['tools_any']}")

    if "tools_all" in expect:
        got = set(result.get("all_tools") or [])
        missing = set(expect["tools_all"]) - got
        add("多跳链完整", not missing, f"missing={sorted(missing)}")

    if "cards_any" in expect:
        got = set(result.get("all_cards") or [])
        hit = got & set(expect["cards_any"])
        add("发射了预期卡片", bool(hit), f"got={sorted(got)} want_any={expect['cards_any']}")

    if expect.get("cards_none"):
        got = set(result.get("all_cards") or [])
        add("没有发射卡片", not got, f"got={sorted(got)}")

    # 产品规则：一次回复最多一张卡（多张会与正文并列抢视线）
    over = [t for t in (result.get("turns") or [])
            if len(t.get("cards") or []) + len(t.get("tables") or []) > 1]
    add("单轮最多一张卡片", not over,
        f"超限轮次={[{ 'cards': t.get('cards'), 'tables': t.get('tables') } for t in over]}")

    for sub in expect.get("must_contain", []):
        add(f"包含「{sub}」", sub.lower() in low, "")

    for sub in expect.get("must_not_contain", []):
        add(f"不含「{sub}」", sub.lower() not in low, "")

    # 产品规则（2026-10-26）：正文不写免责声明，前端也不再固定挂载声明条。
    # 这条对所有用例生效——模型最容易在这里回潮
    add("不含免责声明套话",
        not re.search(r"不构成.{0,8}投资建议|仅供参考|投资需谨慎|风险自负", text), "")

    return checks


def _judge_model():
    from pydantic import BaseModel, Field

    class JudgeResult(BaseModel):
        accuracy: int = Field(description="准确性 1~5")
        completeness: int = Field(description="完整性 1~5")
        relevance: int = Field(description="相关性 1~5")
        timeliness: int = Field(description="时效性 1~5")
        clarity: int = Field(description="表达清晰度 1~5")
        issues: list = Field(default_factory=list, description="发现的问题，中文")
        suggestion: str = Field(default="", description="最关键的一条改进建议，中文")

    return JudgeResult


async def llm_judge(case, result: dict) -> dict:
    """LLM 评委：五维打分 + issues + suggestion。异常时返回全 None 的占位。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from core.llm import get_llm

    final = result.get("final", {})
    query = " -> ".join(t["query"] for t in result.get("turns", []))
    hint = getattr(case, "judge_hint", "") or ""
    tool_lines = []
    for t in final.get("tools", []):
        if t.get("ok"):
            tool_lines.append(f"[工具 {t['tool']} 返回] {t.get('data_head', '')}")
        else:
            tool_lines.append(f"[工具 {t['tool']} 失败] {t.get('error', '')}")
    tools_text = "\n".join(tool_lines)[:4000] or "（本轮无工具数据）"
    prompt = (
        f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"用户问题：\n{query}\n\n"
        f"助手回答：\n{final.get('text', '')}\n\n"
        f"工具返回的真实数据：\n{tools_text}\n\n"
        + (f"【预期行为说明】{hint}\n\n" if hint else "")
        + "请按五个维度打分并给出问题与建议。"
    )
    try:
        # 评委用主力模型：小模型（Qwen2.5-7B）对「婉拒/无数据即为正确行为」这类
        # 预期说明遵循度差，会把正确的拒绝打低分（实测 R-02/K-02）
        llm = get_llm(streaming=False, temperature=0.0)
        model = _judge_model()
        out = await llm.with_structured_output(model).ainvoke([
            SystemMessage(content=_JUDGE_SYSTEM),
            HumanMessage(content=prompt),
        ])
        return {
            "accuracy": out.accuracy, "completeness": out.completeness,
            "relevance": out.relevance, "timeliness": out.timeliness,
            "clarity": out.clarity,
            "issues": list(out.issues or []), "suggestion": out.suggestion or "",
        }
    except Exception as e:  # noqa: BLE001 - 评委故障不阻断评估流程
        return {"error": f"{type(e).__name__}: {e}",
                "accuracy": None, "completeness": None, "relevance": None,
                "timeliness": None, "clarity": None, "issues": [], "suggestion": ""}


async def evaluate(results: list, cases_by_id: dict, with_judge: bool = True,
                   concurrency: int = 4, progress=None) -> list:
    """评估整批结果。with_judge=False 时只跑规则断言（离线快速模式）。"""
    sem = asyncio.Semaphore(max(1, concurrency))
    evals = []

    async def _one(result: dict):
        case = cases_by_id.get(result["id"])
        if case is None:
            return None
        async with sem:
            checks = rule_check(case, result)
            judge = await llm_judge(case, result) if with_judge else {"skipped": True}
            passed = sum(1 for c in checks if c["pass"])
            scores = [judge.get(k) for k in
                      ("accuracy", "completeness", "relevance", "timeliness", "clarity")]
            scores = [s for s in scores if isinstance(s, int)]
            rec = {
                "id": case.id, "category": case.category, "difficulty": case.difficulty,
                "rationale": case.rationale,
                "rule_total": len(checks), "rule_passed": passed,
                "rule_failures": [c for c in checks if not c["pass"]],
                "judge": judge,
                "judge_avg": round(sum(scores) / len(scores), 2) if scores else None,
            }
            if progress:
                j = rec["judge_avg"]
                progress(f"[eval] {case.id}: rules {passed}/{len(checks)}"
                         + (f" judge={j}" if j else ""))
            return rec

    for rec in await asyncio.gather(*[_one(r) for r in results]):
        if rec is not None:
            evals.append(rec)
    return evals


def summarize(evals: list) -> dict:
    """汇总：规则通过率、评委均分、按类型/难度、失败聚类、最差用例。"""
    def _avg(rows, key):
        vals = []
        for r in rows:
            v = r
            for part in key.split("."):
                v = v.get(part) if isinstance(v, dict) else None
                if v is None:
                    break
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(v)
        return round(sum(vals) / len(vals), 2) if vals else None

    total_rules = sum(r["rule_total"] for r in evals)
    passed_rules = sum(r["rule_passed"] for r in evals)
    summary = {
        "cases": len(evals),
        "rule_pass_rate": round(passed_rules / total_rules, 3) if total_rules else None,
        "cases_all_rules_pass": sum(1 for r in evals if r["rule_passed"] == r["rule_total"]),
        "judge_avg": _avg(evals, "judge_avg"),
        "dims": {d: _avg(evals, f"judge.{d}") for d in
                 ("accuracy", "completeness", "relevance", "timeliness", "clarity")},
        "by_category": {}, "by_difficulty": {},
    }
    for dim in ("category", "difficulty"):
        groups = {}
        for r in evals:
            groups.setdefault(r[dim], []).append(r)
        summary["by_category" if dim == "category" else "by_difficulty"] = {
            k: {
                "cases": len(v),
                "rule_pass_rate": round(sum(x["rule_passed"] for x in v)
                                        / max(1, sum(x["rule_total"] for x in v)), 3),
                "judge_avg": _avg(v, "judge_avg"),
            } for k, v in sorted(groups.items())
        }
    # 失败聚类：按 check 名统计
    clusters = {}
    for r in evals:
        for f in r["rule_failures"]:
            clusters.setdefault(f["check"], []).append(r["id"])
    summary["failure_clusters"] = {k: v for k, v in
                                   sorted(clusters.items(), key=lambda kv: -len(kv[1]))}
    # 最差用例：先看规则失败数，再看评委分
    worst = sorted(evals, key=lambda r: (
        -(r["rule_total"] - r["rule_passed"]), r["judge_avg"] if r["judge_avg"] else 99))[:8]
    summary["worst"] = [{
        "id": r["id"], "category": r["category"],
        "rule_fail": r["rule_total"] - r["rule_passed"],
        "judge_avg": r["judge_avg"],
        "failures": [f["check"] for f in r["rule_failures"]],
        "issues": r["judge"].get("issues", [])[:3],
    } for r in worst]
    return summary


def save_evals(evals: list, path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(evals, ensure_ascii=False, indent=1), encoding="utf-8")


def load_evals(path) -> list:
    return json.loads(Path(path).read_text(encoding="utf-8"))
