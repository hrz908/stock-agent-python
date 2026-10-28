"""A股文本语料入库命令行。

    python -m ingest.ashare.cli init-db
    python -m ingest.ashare.cli init-db --file 008_stock_master.sql
    python -m ingest.ashare.cli sync-master
    python -m ingest.ashare.cli collect --source cninfo,cls
    python -m ingest.ashare.cli collect --source em-report --spool ./data/ashare/spool
    python -m ingest.ashare.cli ingest-spool --path ./data/ashare/spool/em-report-xxx.jsonl
    python -m ingest.ashare.cli search "宁德时代 海外产能" --code 300750
"""
import argparse
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from ingest.ashare import config
from ingest.ashare.data_sync import ensure_data_schema, sync_finance, sync_valuation
from ingest.ashare.embedder import Embedder
from ingest.ashare.master import ensure_master_schema, sync_master
from ingest.ashare.pipeline import Pipeline
from ingest.ashare.sources import build_source, source_names
from ingest.ashare.store import Store
from ingest.ashare.universe import ensure_universe_schema, sync_top_universe, top_codes
from ingest.ashare.util import (
    CN_TZ,
    now_cn,
    parse_date_arg,
    reconfigure_stdio,
    setup_logging,
    split_csv,
    to_day,
)

logger = logging.getLogger("ashare")


def _open_store(need_schema: bool = True) -> Store:
    store = Store()
    store.connect()
    if need_schema:
        store.ensure_schema()
    return store


def _resolve_sources(text: str) -> list[str]:
    names = split_csv(text or "all")
    if not names or "all" in names:
        return [n for n in config.DAEMON_ORDER if n != "em-news"]
    unknown = [n for n in names if n not in source_names()]
    if unknown:
        raise SystemExit(f"未知数据源：{', '.join(unknown)}（可选：{', '.join(source_names())}）")
    return names


def _window(store: Store, source, args) -> tuple[datetime, datetime]:
    until = parse_date_arg(args.until) or now_cn()
    since = parse_date_arg(args.since)
    if since:
        return since, until

    watermark = store.get_watermark(source.name) if store else None
    if watermark:
        if source.watermark_mode != "window":
            overlap = config.LATEST_OVERLAP_HOURS
        elif source.name == "em-report":
            overlap = config.REPORT_OVERLAP_HOURS
        else:
            overlap = config.WINDOW_OVERLAP_HOURS
        since = watermark - timedelta(hours=overlap)
    else:
        since = until - timedelta(days=source.default_backfill_days)
    if since >= until:
        since = until - timedelta(days=1)
    return since, until


def _spool_file(base: Path, source: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{source}-{now_cn():%Y%m%d-%H%M%S}.jsonl"


def _migration_paths(names: list[str]) -> list[Path]:
    """按文件名解析迁移：容器内 /app/migrations、本地仓库两处找。"""
    env = os.getenv("ASHARE_MIGRATION")
    if env:
        return [Path(env)]
    # 仓库根 = src/ingest/ashare/cli.py 往上四级
    repo_root = Path(__file__).resolve().parents[3]
    dirs = [Path("/app/migrations"), repo_root / "sql" / "migrations"]
    out = []
    for name in names:
        path = Path(name)
        if path.is_absolute() or path.exists():
            out.append(path)
            continue
        hit = next((d / name for d in dirs if (d / name).exists()), None)
        if hit is None:
            raise SystemExit(f"找不到迁移文件：{name}（查找目录：{'、'.join(map(str, dirs))}）")
        out.append(hit)
    return out


# ---------------------------------------------------------------------------
def cmd_init_db(args) -> int:
    migrations = _migration_paths(args.file or ["007_ashare_corpus.sql"])
    store = Store()
    store.connect()
    try:
        for migration in migrations:
            sql = migration.read_text(encoding="utf-8")
            # psycopg 的扩展协议只接受单条语句，按分号拆开逐条执行（这些迁移无函数体）
            body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
            for statement in body.split(";"):
                if statement.strip():
                    store.conn.execute(statement)
            store.conn.commit()
            logger.info("迁移已执行：%s", migration)
    finally:
        store.close()
    return 0


def cmd_sync_master(args) -> int:
    """同步股票清单 / 行业 / 概念（迁移 008 的三张表）。"""
    only = split_csv(args.only) or None
    unknown = [x for x in (only or []) if x not in ("basic", "industry", "concept")]
    if unknown:
        raise SystemExit(f"未知主数据类型：{', '.join(unknown)}（可选 basic/industry/concept）")
    store = Store()
    store.connect()
    try:
        ensure_master_schema(store)
        stats = sync_master(
            store,
            only=only,
            concept_limit=args.concept_limit,
            industry_limit=args.industry_limit,
        )
    finally:
        store.close()
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    return 0


def _collect_codes(store, args) -> list[str] | None:
    """--codes 显式指定；--universe 读当日热门池；两者同时给时取交集。"""
    explicit = split_csv(args.codes) if getattr(args, "codes", None) else None
    if not getattr(args, "universe", False):
        return explicit
    pool = top_codes(store)
    if not pool:
        raise SystemExit(f"热门池为空：先执行 sync-universe --top {config.UNIVERSE_TOP_N}")
    if explicit:
        keep = set(pool)
        return [c for c in explicit if c in keep]
    return pool


def cmd_collect(args) -> int:
    # dry-run 且不按池子采集时不必连库；--universe 要把池子读出来
    store = _open_store() if (not args.dry_run or args.universe) else None
    embedder = None
    if not args.dry_run and not args.spool:
        embedder = Embedder()
    pipeline = Pipeline(store, embedder, force=args.force)
    codes = _collect_codes(store, args)
    if args.universe:
        logger.info("按热门池采集：%d 只", len(codes or []))

    results = []
    try:
        for name in _resolve_sources(args.source):
            source = build_source(name)
            since, until = _window(store, source, args)
            spool_path = _spool_file(Path(args.spool), name) if args.spool else None
            logger.info("开始采集 %s：%s ~ %s%s", name, to_day(since), to_day(until),
                        f" -> {spool_path}" if spool_path else "")
            stats = pipeline.run_source(
                source, since, until,
                limit=args.limit, codes=codes, dry_run=args.dry_run, spool_path=spool_path,
            )
            results.append(stats.as_dict())
    finally:
        if store:
            store.close()

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_sync_universe(args) -> int:
    """刷新成交额 Top N 股票池（采集范围的唯一来源）。"""
    store = _open_store()
    try:
        ensure_universe_schema(store)
        stats = sync_top_universe(store, args.top)
    finally:
        store.close()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def _iter_windows(since, until, days: int):
    start = since
    while start <= until:
        end = min(start + timedelta(days=days - 1), until)
        yield start, end
        start = end + timedelta(days=1)


def cmd_backfill(args) -> int:
    """按窗口回填历史语料（默认五年），断点记录在 ashare_backfill_progress。

    不推 ashare_sync_state 水位：回填是按窗口从早到晚跑的，推水位会让日常增量
    误以为「已经采到最新」，而实际上中间还有窗口没跑完。
    """
    store = _open_store()
    sources = _resolve_sources(args.source)
    codes = _collect_codes(store, args)
    if not codes:
        store.close()
        raise SystemExit("回填必须限定股票范围：--universe 或 --codes")

    shard_tag = ""
    if args.shard:
        try:
            index, total = (int(x) for x in args.shard.split("/"))
        except ValueError as exc:
            store.close()
            raise SystemExit("--shard 格式应为 i/n，如 1/3") from exc
        if not (1 <= index <= total):
            store.close()
            raise SystemExit("--shard 的 i 必须在 1..n 之间")
        codes = codes[index - 1::total]
        shard_tag = f"#{index}-{total}"
        logger.info("分片 %d/%d：本片 %d 只", index, total, len(codes))

    until = (parse_date_arg(args.until) or now_cn()).date()
    since = (parse_date_arg(args.since) or (now_cn() - timedelta(days=365 * args.years))).date()
    if since > until:
        store.close()
        raise SystemExit(f"起始日 {since} 晚于结束日 {until}")

    # 断点按采集范围隔离：热门池把池子大小和分片号一起编进 scope。
    # 只记 "universe" 会让两种情形误跳过已完成窗口：换了 N 重跑（300 ↔ 500），
    # 以及多片并行时某片跳过另一片标 done 的窗口（它自己分配的代码将永远缺失，且不报错）。
    scope = f"universe{config.UNIVERSE_TOP_N}" if args.universe else "codes:" + hashlib.sha1(
        ",".join(sorted(codes)).encode("utf-8")).hexdigest()[:12]
    scope += shard_tag

    pipeline = Pipeline(store, Embedder(), force=args.force)
    summary: list[dict] = []
    try:
        for name in sources:
            source = build_source(name)
            done = store.backfill_done(name, scope)
            total_docs = total_chunks = skipped_windows = 0
            for start, end in _iter_windows(since, until, args.window_days):
                if start.isoformat() in done:
                    skipped_windows += 1
                    continue
                logger.info("回填 %s：%s ~ %s（%d 只）", name, start, end, len(codes))
                stats = pipeline.run_source(
                    source,
                    datetime.combine(start, dt_time(0, 0), tzinfo=CN_TZ),
                    datetime.combine(end, dt_time(23, 59, 59), tzinfo=CN_TZ),
                    limit=args.limit,
                    codes=codes,
                    advance_watermark=False,
                )
                # 只要有一条没入库就标 failed，下次重跑补上：
                # 已入库的条目靠 source_hash 直接跳过，重跑代价只是一次翻页。
                # 原判据「有失败但已写入」会把「缺一篇年报」的窗口也标成 done，永久漏采
                status = "failed" if (stats.failed or stats.error) else "done"
                store.mark_backfill(name, scope, start, end, status=status,
                                    docs=stats.written, chunks=stats.chunks)
                total_docs += stats.written
                total_chunks += stats.chunks
            summary.append({
                "source": name, "docs": total_docs, "chunks": total_chunks,
                "windows_skipped": skipped_windows,
            })
    finally:
        store.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_prune(args) -> int:
    """清理语料：两个维度可单用可叠加（默认只统计，--yes 才真删）。

    --older-than N：删早于 N 天的，用来把库固定在「最近 N 年」；
    --outside-universe：删热门池之外个股的语料，用来把范围固定在 Top N；
    --grace-days N：池外判定给 N 天宽限（最近 N 天进过池的代码不删），防止边界股抖动。
    """
    store = _open_store()
    result: dict = {}
    try:
        if args.older_than:
            result["older_than"] = store.prune_older_than(args.older_than, apply=args.yes)
        if args.outside_universe or not args.older_than:
            result["outside_universe"] = store.prune_outside_universe(
                apply=args.yes, grace_days=args.grace_days)
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.yes and any(item["matched"] for item in result.values()):
        print("加 --yes 才会真正删除（含 chunks 级联）")
    return 0


def cmd_forget_backfill(args) -> int:
    """删除指定 scope 的回填断点。

    换池子大小时 scope 会从 universe 变成 universe300#i-n，旧断点虽然永远不会再命中，
    但排查「这个窗口为什么被跳过」时会造成误导。只删断点，不动已入库的语料。
    """
    store = _open_store(need_schema=False)
    try:
        cur = store.conn.execute(
            "DELETE FROM ashare_backfill_progress WHERE scope = %s", (args.scope,)
        )
        store.conn.commit()
        deleted = cur.rowcount or 0
    finally:
        store.close()
    print(f"已删除 scope={args.scope} 的回填断点 {deleted} 行（已入库语料不受影响）")
    return 0


def cmd_ingest_spool(args) -> int:
    store = _open_store()
    pipeline = Pipeline(store, Embedder(), force=args.force)
    try:
        stats = pipeline.ingest_spool(Path(args.path), dry_run=args.dry_run)
    finally:
        store.close()
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_reembed(args) -> int:
    store = _open_store()
    embedder = Embedder()
    try:
        doc_ids = store.stale_doc_ids(config.EMBED_MODEL, args.limit)
        logger.info("待重嵌文档：%d", len(doc_ids))
        done = failed = 0
        for index, doc_id in enumerate(doc_ids, 1):
            rows = store.load_chunks(doc_id)
            if not rows:
                continue
            try:
                vectors = embedder.embed([r["content"] for r in rows])
                store.update_embeddings(list(zip([r["id"] for r in rows], vectors, strict=True)),
                                        doc_id, config.EMBED_MODEL)
                done += 1
            except Exception as exc:
                failed += 1
                logger.error("重嵌失败 %s：%s", doc_id, exc)
            if index % 20 == 0:
                logger.info("进度 %d/%d", index, len(doc_ids))
        logger.info("重嵌完成：成功 %d，失败 %d", done, failed)
    finally:
        store.close()
    return 0


def cmd_stats(args) -> int:
    store = _open_store()
    try:
        data = store.stats()
    finally:
        store.close()
    print(f"文档 {data['docs']} 篇，向量块 {data['chunks']} 个")
    print(f"{'source':<12}{'source_type':<20}{'docs':>8}{'chunks':>10}  {'last_pub':<12}{'q':>6}")
    for row in data["rows"]:
        last = row["last_pub"].strftime("%Y-%m-%d") if row["last_pub"] else "-"
        print(f"{row['source']:<12}{row['source_type']:<20}{row['docs']:>8}{row['chunks']:>10}  "
              f"{last:<12}{float(row['avg_quality'] or 0):>6.2f}")
    return 0


def _human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def cmd_storage(args) -> int:
    store = _open_store()
    try:
        data = store.storage_report()
    finally:
        store.close()

    chunk_row = next((t for t in data["tables"] if t["name"] == "chunks"), None)
    chunk_bytes = float(chunk_row["bytes"]) if chunk_row else 0.0
    chunks = data["chunks"]
    # 表占用包含文件 ingest 的块；按全库块数算均值才不会被高估
    per_chunk = chunk_bytes / data["total_chunks"] if data["total_chunks"] else 0.0
    vector_bytes = data["dim"] * 4

    print(f"语料：{data['docs']} 篇文档 / {chunks} 个向量块（全库 {data['total_chunks']} 块）")
    print(f"\n{'对象':<28}{'占用':>12}")
    for row in data["tables"]:
        print(f"{row['name']:<28}{_human_bytes(float(row['bytes'])):>12}")
    for row in data["indexes"]:
        print(f"  索引 {row['name']:<22}{_human_bytes(float(row['bytes'])):>12}")

    if not chunks:
        print("\n还没有 A股向量，入库后再看更有意义")
        return 0
    print(f"\n平均 {_human_bytes(per_chunk)}/块（含索引与 TOAST）；"
          f"float4 {data['dim']} 维向量本身 {_human_bytes(vector_bytes)}/块")
    for target in (1_000_000, 10_000_000):
        print(f"  按当前均值估算 {target // 10_000} 万块 ≈ {_human_bytes(per_chunk * target)}")
    if per_chunk * 10_000_000 > 100 * 1024**3:
        print("\n建议：千万块量级已超过 100GB。优先考虑 halfvec（向量减半，需重建 HNSW），"
              "或在有迁移窗口时降到 1024 维；冷数据可按年份归档到独立表。")
    else:
        print("\n结论：当前块大小下，千万块仍在 100GB 以内，维持 vector(1536) 即可。")
    return 0


def cmd_search(args) -> int:
    store = _open_store()
    try:
        vector = Embedder().embed([args.query], is_query=True)[0]
        rows = store.search(vector, k=args.top_k, source_type=args.type,
                            code=args.code, since=parse_date_arg(args.since))
    finally:
        store.close()

    if not rows:
        print("没有命中")
        return 0
    for i, row in enumerate(rows, 1):
        pub = row["pub_time"].strftime("%Y-%m-%d") if row["pub_time"] else "-"
        print(f"\n[{i}] {row['similarity']:.3f} | {row['source_type']} | {pub} | {row['title']}")
        print(f"    {row['url']}")
        print("    " + row["content"][:200].replace("\n", " "))
    return 0


def cmd_daemon(args) -> int:
    store = _open_store()
    pipeline = Pipeline(store, Embedder(), force=False)
    next_run: dict[str, float] = {name: 0.0 for name in config.DAEMON_ORDER}
    logger.info("守护进程启动：%s", ", ".join(config.DAEMON_ORDER))
    try:
        while True:
            now = time.monotonic()
            for name in config.DAEMON_ORDER:
                if now < next_run[name]:
                    continue
                interval = config.DAEMON_INTERVALS.get(name, 3600)
                try:
                    source = build_source(name)
                    since, until = _window(store, source, argparse.Namespace(since=None, until=None))
                    if name == "em-news" and not split_csv(config.NEWS_CODES):
                        next_run[name] = time.monotonic() + interval
                        continue
                    pipeline.run_source(source, since, until)
                    next_run[name] = time.monotonic() + interval
                except Exception as exc:
                    logger.error("源 %s 执行异常：%s", name, exc)
                    next_run[name] = time.monotonic() + min(interval, 300)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("收到退出信号")
    finally:
        store.close()
    return 0


def _data_codes(store, args) -> list[str]:
    """业务表的采集范围与向量语料同一口径：默认当前热门池，也可显式给代码。"""
    explicit = split_csv(args.codes) if getattr(args, "codes", None) else None
    if explicit:
        return explicit
    pool = top_codes(store)
    if not pool:
        raise SystemExit(f"热门池为空：先执行 sync-universe --top {config.UNIVERSE_TOP_N}")
    return pool


def cmd_sync_finance(args) -> int:
    """把报告期财务指标写进业务表（stock_finance_indicator）。"""
    store = _open_store(need_schema=False)
    try:
        ensure_data_schema(store)
        codes = _data_codes(store, args)
        logger.info("同步财务指标：%d 只，最近 %d 年", len(codes), args.years)
        stats = sync_finance(store, codes, years=args.years)
    finally:
        store.close()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_sync_valuation(args) -> int:
    """把每日估值写进业务表（stock_valuation_daily）。"""
    store = _open_store(need_schema=False)
    try:
        ensure_data_schema(store)
        codes = _data_codes(store, args)
        logger.info("同步估值：%d 只，每股 %d 个交易日", len(codes), args.days)
        stats = sync_valuation(store, codes, days=args.days)
    finally:
        store.close()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ashare", description="A股文本语料向量化入库")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init-db", help="执行迁移 SQL（默认 007_ashare_corpus.sql）")
    init.add_argument("--file", action="append",
                      help="迁移文件名或路径，可重复；按给定顺序执行，如 "
                           "--file 008_stock_master.sql --file 009_stock_finance_indicator.sql")
    init.set_defaults(func=cmd_init_db)

    master = sub.add_parser("sync-master", help="同步股票清单/行业/概念（迁移 008 的三张表）")
    master.add_argument("--only", default="basic,industry,concept",
                        help="逗号分隔：basic,industry,concept；顺序固定，行业/概念依赖清单先入库")
    master.add_argument("--industry-limit", type=int, default=0, help="只同步前 N 个行业板块（0=全部）")
    master.add_argument("--concept-limit", type=int, default=0,
                        help="只同步前 N 个概念板块（0=全部，全量约 4~5 分钟）")
    master.set_defaults(func=cmd_sync_master)

    fin = sub.add_parser("sync-finance", help="同步报告期财务指标到业务表（迁移 009）")
    fin.add_argument("--codes", help="逗号分隔代码；留空=当前热门池（与向量语料同一口径）")
    fin.add_argument("--years", type=int, default=config.FINANCE_YEARS,
                     help=f"覆盖最近 N 年报告期（默认 {config.FINANCE_YEARS}）")
    fin.set_defaults(func=cmd_sync_finance)

    val = sub.add_parser("sync-valuation", help="同步每日估值到业务表（迁移 014）")
    val.add_argument("--codes", help="逗号分隔代码；留空=当前热门池")
    val.add_argument("--days", type=int, default=config.VALUATION_DAYS,
                     help=f"每只回看交易日数（默认 {config.VALUATION_DAYS}，补历史用 250）")
    val.set_defaults(func=cmd_sync_valuation)

    collect = sub.add_parser("collect", help="采集并向量化入库")
    collect.add_argument("--source", default="all", help=f"逗号分隔，可选 {','.join(source_names())} 或 all")
    collect.add_argument("--since", help="起始日期 YYYY-MM-DD（默认按水位）")
    collect.add_argument("--until", help="结束日期 YYYY-MM-DD（默认当前）")
    collect.add_argument("--limit", type=int, help="本次最多处理多少条")
    collect.add_argument("--codes", help="只采集指定股票代码，逗号分隔")
    collect.add_argument("--universe", action="store_true",
                         help=f"按成交额 Top N 股票池采集（N 见 ASHARE_UNIVERSE_TOP_N，默认 {config.UNIVERSE_TOP_N}）")
    collect.add_argument("--force", action="store_true", help="忽略内容未变早退，强制重跑")
    collect.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    collect.add_argument("--spool", help="只采集清洗，落 JSONL 目录（供离线侧 ingest-spool）")
    collect.set_defaults(func=cmd_collect)

    ingest = sub.add_parser("ingest-spool", help="离线入库 spool 文件")
    ingest.add_argument("--path", required=True)
    ingest.add_argument("--force", action="store_true")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.set_defaults(func=cmd_ingest_spool)

    uni = sub.add_parser("sync-universe", help="刷新成交额 Top N 股票池（采集范围）")
    uni.add_argument("--top", type=int, default=config.UNIVERSE_TOP_N)
    uni.set_defaults(func=cmd_sync_universe)

    back = sub.add_parser("backfill", help="按窗口回填历史语料（默认五年），断点可续跑")
    back.add_argument("--source", default="all", help="逗号分隔，默认 all（不含 em-news）")
    back.add_argument("--years", type=int, default=config.BACKFILL_YEARS)
    back.add_argument("--since", help="起始日 YYYY-MM-DD（默认 N 年前）")
    back.add_argument("--until", help="结束日 YYYY-MM-DD（默认今天）")
    back.add_argument("--window-days", type=int, default=config.BACKFILL_WINDOW_DAYS,
                      help="单窗口天数，撞上源的翻页上限时调小")
    back.add_argument("--universe", action="store_true", help="按热门股票池回填")
    back.add_argument("--codes", help="显式指定代码（与 --universe 同时给时取交集）")
    back.add_argument("--shard", help="分片，如 1/3：多容器并行回填时用")
    back.add_argument("--limit", type=int, help="单窗口最多处理多少条")
    back.add_argument("--force", action="store_true")
    back.set_defaults(func=cmd_backfill)

    prune = sub.add_parser("prune", help="按时间/范围清理语料（默认只统计）")
    prune.add_argument("--older-than", type=int, metavar="DAYS",
                       help="删除发布时间早于 N 天前的语料（保留无发布时间的公共语料）")
    prune.add_argument("--outside-universe", action="store_true",
                       help="删除热门池之外个股的语料；单独给 --older-than 时不执行")
    prune.add_argument("--grace-days", type=int, default=0, metavar="DAYS",
                       help="池外判定给 N 天宽限：最近 N 天进过池的代码不删（防边界股抖动）")
    prune.add_argument("--yes", action="store_true", help="确认删除")
    prune.set_defaults(func=cmd_prune)

    forget = sub.add_parser("forget-backfill", help="删除指定 scope 的回填断点（不动已入库语料）")
    forget.add_argument("--scope", default="universe",
                        help="要删除的 scope，默认 universe（换池子大小前的旧口径）")
    forget.set_defaults(func=cmd_forget_backfill)

    reembed = sub.add_parser("reembed", help="按当前模型重嵌陈旧文档")
    reembed.add_argument("--limit", type=int)
    reembed.set_defaults(func=cmd_reembed)

    sub.add_parser("stats", help="按来源/类型统计").set_defaults(func=cmd_stats)
    sub.add_parser("storage", help="表/索引占用与千万块容量评估").set_defaults(func=cmd_storage)

    search = sub.add_parser("search", help="检索自检（不需要 agent）")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=8)
    search.add_argument("--type", help="source_type 过滤")
    search.add_argument("--code", help="股票代码过滤")
    search.add_argument("--since", help="发布时间下限 YYYY-MM-DD")
    search.set_defaults(func=cmd_search)

    sub.add_parser("daemon", help="常驻增量采集").set_defaults(func=cmd_daemon)
    return parser


def main(argv=None) -> int:
    reconfigure_stdio()  # 必须在 argparse 之前：--help 的中文先于日志初始化输出
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    logger.info("模型=%s dim=%s 库=%s", config.EMBED_MODEL, config.EMBED_DIM,
                config.DATABASE_URL.rsplit("@", 1)[-1])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
