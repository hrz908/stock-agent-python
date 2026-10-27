#!/usr/bin/env python
"""检索评测集生成：抽样 chunk → small_llm 反向出题 → retrieval_cases.jsonl。

用法（Windows 控制台务必带 -X utf8）：
  python -X utf8 tests/benchmark/gen_retrieval_cases.py --chunks 40
  python -X utf8 tests/benchmark/gen_retrieval_cases.py --chunks 20 --append

两条口径：
- gold_chunk_ids 由「出题来源 chunk」直接确定，不需要人工标注；
  人工只需抽检问题措辞是否可检索（约 30% 抽检即可）。
- 机器出的题天然贴着原文（术语复现率高），真实召回率会偏高一点；
  hard 档偏弱，必要时手工补几条口语化改写。

负样本（gold 为空）是硬编码的语料外问题，用于测误命中与误升级。
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

HERE = Path(__file__).resolve().parent                 # .../tests/benchmark
sys.path.insert(0, str(HERE.parents[2] / "src"))  # src/：gateway/agent/ingest 包根
sys.path.insert(0, str(HERE.parents[1]))               # tests/

DEFAULT_OUT = HERE / "retrieval_cases.jsonl"

# 语料外问题：不该被投教知识库命中，命中即为误命中
NEGATIVES = [
    "比特币今天多少钱一枚",
    "美元兑人民币今天的中间价是多少",
    "帮我看看黄金期货主力合约的持仓量",
    "明天大盘会涨还是会跌",
    "推荐一只下周能涨的股票",
    "今年新发的国债利率是多少",
    "腾讯控股今天的港股收盘价",
    "上海哪家券商开户佣金最低",
    "科创板第八批上市企业名单",
    "原油期货今晚的夜盘走势",
    "我账户里的持仓成本怎么算出来的",
    "明天打新哪只股票中签率高",
    "以太坊现在市值多少",
    "日本央行最新的议息决议",
    "帮我写一份私募基金备案材料",
    "存量房贷利率下调到多少了",
    "伦敦金现今天开盘价",
    "帮我算一下我的个人所得税",
    "美股英伟达最新的财报日期",
    "周末去哪个城市旅游合适",
]

_SYSTEM = (
    "你是检索评测集的出题器。给定一段股票投教/研究资料片段，"
    "你要出「用户会真实提出的问题」。只输出 JSON 数组，不要解释。"
)

_PROMPT = """请针对下面这段资料出 {n} 个问题，要求：
1. 只用这段内容就能回答，禁止问实时行情、股价、涨跌幅、具体日期等需要取数的内容；
2. 口语化中文提问，不要照抄标题或堆砌术语；
3. 难度要有区分：easy=术语直问；medium=换个说法或需跨句合并；hard=需要理解因果或多概念对比；
4. 只输出 JSON 数组，形如 [{{"q":"...","difficulty":"easy"}}]。

资料（来自《{title}》）：
{content}
"""


def _parse_array(text: str) -> list:
    """从模型输出里抠出 JSON 数组（容忍 ```json 围栏与前后废话）。"""
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _sample_chunks(dsn: str, n: int) -> list:
    """按文档去重抽样：每个文档先取 1 个 chunk，保证题材分散而不是单篇刷屏。"""
    import psycopg
    sql = """
    WITH c AS (
      SELECT ch.id::text AS id, ch.content, d.title,
             ROW_NUMBER() OVER (PARTITION BY ch.document_id ORDER BY random()) AS rn
      FROM chunks ch JOIN documents d ON d.id = ch.document_id
      WHERE length(ch.content) >= 200
    )
    SELECT id, content, title FROM c WHERE rn = 1 ORDER BY random() LIMIT %s
    """
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(sql, (n,)).fetchall()
    return [{"id": r[0], "content": r[1], "title": r[2]} for r in rows]


async def _ask(llm, sem, item: dict, per_chunk: int) -> list:
    from langchain_core.messages import HumanMessage, SystemMessage
    async with sem:
        try:
            resp = await llm.ainvoke([
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=_PROMPT.format(
                    n=per_chunk, title=item["title"], content=item["content"][:1200])),
            ])
        except Exception as e:  # noqa: BLE001 - 单条出题失败不阻断整批
            print(f"[warn] {item['title'][:20]} 出题失败: {type(e).__name__}: {e}")
            return []
    out = []
    for q in _parse_array(resp.content if isinstance(resp.content, str) else ""):
        text = str(q.get("q", "")).strip()
        diff = str(q.get("difficulty", "")).strip().lower()
        if not text or diff not in ("easy", "medium", "hard"):
            continue
        out.append({
            "query": text,
            "rewritten": text,  # 单轮无历史，rewrite 节点原样返回；多轮用例须手工填真实改写
            "gold_chunk_ids": [item["id"]],
            "difficulty": diff,
            "category": "retrieval",
            "source_title": item["title"],
        })
    return out


def _next_qid(existing: list, prefix: str) -> int:
    nums = [int(c["qid"].split("-")[-1]) for c in existing
            if str(c.get("qid", "")).startswith(prefix) and c["qid"].split("-")[-1].isdigit()]
    return max(nums, default=0) + 1


async def main_async(args) -> None:
    from benchmark.runner import apply_inproc_env
    apply_inproc_env(verbose=True)

    from agent.config import DATABASE_URL
    from agent.core.llm import get_small_llm

    out_path = Path(args.out)
    existing = []
    if args.append and out_path.exists():
        existing = [
            json.loads(line)
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        print(f"[append] 已有 {len(existing)} 条")

    chunks = _sample_chunks(DATABASE_URL, args.chunks)
    print(f"[sample] 命中 {len(chunks)} 个文档的片段")
    if not chunks:
        print("库里没有可用 chunk，先跑 ingest")
        return

    llm = get_small_llm(streaming=False, temperature=0.7)
    sem = asyncio.Semaphore(max(1, args.concurrency))
    batches = await asyncio.gather(*[_ask(llm, sem, c, args.per_chunk) for c in chunks])

    seen = {c["query"].strip() for c in existing}
    rno = _next_qid(existing, "R")
    new_cases = []
    for batch in batches:
        for q in batch:
            key = q["query"].strip()
            if key in seen:
                continue
            seen.add(key)
            q["qid"] = f"R-{rno:03d}"
            rno += 1
            new_cases.append(q)

    if not args.no_negatives:
        nno = _next_qid(existing, "N")
        for text in NEGATIVES:
            if text in seen:
                continue
            seen.add(text)
            q = {"qid": f"N-{nno:03d}", "query": text, "rewritten": text,
                 "gold_chunk_ids": [], "negative": True,
                 "difficulty": "easy", "category": "retrieval", "source_title": ""}
            nno += 1
            new_cases.append(q)

    all_cases = existing + new_cases
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for c in all_cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    pos = [c for c in all_cases if not c.get("negative")]
    dist = {}
    for c in pos:
        dist[c["difficulty"]] = dist.get(c["difficulty"], 0) + 1
    print(f"DONE -> {out_path}")
    print(f"  正样本 {len(pos)} 条（难度 {dist}），负样本 {len(all_cases) - len(pos)} 条")
    print("  下一步：抽检 30% 问题措辞是否可检索，然后跑 sweep_retrieval.py collect")


def main():
    ap = argparse.ArgumentParser(description="检索评测集生成（chunk 反向出题）")
    ap.add_argument("--chunks", type=int, default=40, help="抽样的 chunk 数（按文档去重，每文档 1 个）")
    ap.add_argument("--per-chunk", type=int, default=3, help="每个 chunk 出几道题")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--append", action="store_true", help="追加到已有文件（qid 顺延）")
    ap.add_argument("--no-negatives", action="store_true", help="不写负样本")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
