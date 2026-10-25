"""流水线：取正文 → 清洗 → 门禁 → 去重 → 分块 → 嵌入 → 入库。

去重放在嵌入之前：年报动辄 300 页，先算指纹再决定是否调模型，
比「先嵌入再发现重复」省掉一个数量级的算力。
"""
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ashare import clean, config, quality
from ashare.chunking import chunk_document
from ashare.embedder import Embedder
from ashare.net import HttpClient, download_pdf
from ashare.sources.base import BaseSource
from ashare.store import Store
from ashare.types import PERIODIC_TYPES, Chunk, RawDoc, RunStats
from ashare.util import now_cn, sha1_hex, to_day

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^\d{6}$")


class SkipDoc(Exception):
    """文档被主动跳过（不是失败），携带人类可读原因。"""


@dataclass(slots=True)
class Prepared:
    raw: RawDoc
    body: str
    fingerprint: str
    simhash: int
    bands: list[int]
    quality: float
    source_hash: str
    from_pdf: bool


class Pipeline:
    def __init__(self, store: Store | None, embedder: Embedder | None, *, force: bool = False):
        self.store = store
        self.embedder = embedder
        self.force = force
        self.http = HttpClient()
        self._pdf_left = config.PDF_MAX_PER_RUN

    # ------------------------------------------------------------------ 对外
    def run_source(
        self,
        source: BaseSource,
        since: datetime,
        until: datetime,
        *,
        limit: int | None = None,
        codes=None,
        dry_run: bool = False,
        spool_path: Path | None = None,
    ) -> RunStats:
        stats = RunStats(source=source.name)
        error: str | None = None
        run_id = None if dry_run else self.store.start_run(source.name)
        self._pdf_left = config.PDF_MAX_PER_RUN

        try:
            for raw in source.fetch(since, until, limit=limit, codes=codes):
                stats.fetched += 1
                try:
                    if spool_path:
                        prepared = self._prepare(raw, dry_run=dry_run)
                        self._append_spool(spool_path, prepared)
                        stats.written += 1
                        logger.info("已落 spool：%s", prepared.raw.title[:50])
                    else:
                        outcome, chunk_count = self._process(raw, dry_run=dry_run)
                        stats.chunks += chunk_count
                        if outcome == "written":
                            stats.written += 1
                        elif outcome == "skipped":
                            stats.skipped += 1
                    # 水位只跟随处理成功的条目：失败的最新一条不能把水位顶过去
                    if raw.pub_time and (stats.last_pub is None or raw.pub_time > stats.last_pub):
                        stats.last_pub = raw.pub_time
                except SkipDoc as exc:
                    stats.skipped += 1
                    if raw.pub_time and (stats.last_pub is None or raw.pub_time > stats.last_pub):
                        stats.last_pub = raw.pub_time
                    logger.debug("跳过（%s）：%s", exc, raw.title[:50])
                except Exception:  # 单条失败不能拖垮整源
                    stats.failed += 1
                    logger.exception("处理失败：%s", raw.title[:50])
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error("采集源 %s 中断：%s", source.name, error)
        finally:
            if self.store and not dry_run:
                self.store.finish_run(run_id, stats, error)
                self._advance_watermark(source, until, stats, error, dry_run)

        logger.info(
            "源 %s 完成：抓到 %d，入库 %d，跳过 %d，失败 %d，chunk %d",
            source.name, stats.fetched, stats.written, stats.skipped, stats.failed, stats.chunks,
        )
        return stats

    def ingest_spool(self, path: Path, *, dry_run: bool = False) -> RunStats:
        """离线侧：只做分块/嵌入/入库，不再访问网络。"""
        stats = RunStats(source="spool")
        for raw in iter_spool(path):
            stats.fetched += 1
            try:
                outcome, chunk_count = self._process(raw, dry_run=dry_run, trust_body=True)
                stats.chunks += chunk_count
                if outcome == "written":
                    stats.written += 1
                else:
                    stats.skipped += 1
            except SkipDoc as exc:
                stats.skipped += 1
                logger.debug("跳过（%s）：%s", exc, raw.title[:50])
            except Exception:
                stats.failed += 1
                logger.exception("入库失败：%s", raw.title[:50])
        logger.info("spool 完成：%s 入库 %d，跳过 %d，失败 %d，chunk %d",
                    path.name, stats.written, stats.skipped, stats.failed, stats.chunks)
        return stats

    # ------------------------------------------------------------------ 单条
    def _process(self, raw: RawDoc, *, dry_run: bool = False, trust_body: bool = False) -> tuple[str, int]:
        prepared = self._prepare(raw, dry_run=dry_run, trust_body=trust_body)
        chunks: list[Chunk] = chunk_document(
            prepared.body,
            {
                "stock_codes": raw.stock_codes,
                "stock_names": raw.stock_names,
                "source_type": raw.source_type,
                "pub_date": to_day(raw.pub_time),
            },
            max_chars=config.CHUNK_MAX_CHARS,
            min_chars=config.CHUNK_MIN_CHARS,
            overlap=config.CHUNK_OVERLAP_CHARS,
        )
        if not chunks:
            raise SkipDoc("no_chunks")

        if dry_run:
            logger.info("[dry-run] %s | %s | %d chunks", raw.source_type, raw.title[:50], len(chunks))
            return "written", len(chunks)

        vectors = self.embedder.embed([c.content for c in chunks])
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.embedding = vector
            chunk.metadata = self._chunk_metadata(prepared, chunk, len(chunks))

        meta = {
            "source": raw.source,
            "source_id": raw.source_id,
            "source_type": raw.source_type,
            "title": raw.title,
            "url": raw.url,
            "pub_time": raw.pub_time,
            "stock_codes": raw.stock_codes,
            "stock_names": raw.stock_names,
            "industry": raw.industry,
            "org": raw.org,
            "author": raw.author,
            "rating": raw.rating,
            "event_type": raw.event_type,
            "quality_score": prepared.quality,
            "fingerprint": prepared.fingerprint,
            "simhash": prepared.simhash,
            "simhash_bands": prepared.bands,
            "content_bytes": len(prepared.body.encode("utf-8")),
            "source_hash": prepared.source_hash,
            "extra": raw.extra,
        }
        doc_id = self.store.upsert_document(
            source_path=raw.source_path,
            title=raw.title,
            mime_type="text/markdown" if prepared.from_pdf else "text/plain",
            content_hash=sha1_hex(raw.title, prepared.body),
            embed_model=config.EMBED_MODEL,
            meta=meta,
            chunks=chunks,
        )
        if doc_id is None:
            return "skipped", 0
        logger.info("入库 %s｜%s｜%d chunks｜q=%.2f",
                    raw.source_type, raw.title[:40], len(chunks), prepared.quality)
        return "written", len(chunks)

    def _prepare(self, raw: RawDoc, *, dry_run: bool = False, trust_body: bool = False) -> Prepared:
        raw.title = re.sub(r"\s+", " ", clean.normalize(raw.title))[:200]
        raw.stock_codes = [c.zfill(6) for c in (raw.stock_codes or []) if _CODE_RE.match(str(c).zfill(6))]
        raw.stock_names = [n for n in (raw.stock_names or []) if n]
        raw.extra = json.loads(json.dumps(raw.extra or {}, ensure_ascii=False, default=str))

        src_hash = quality.source_hash(raw.source_id, raw.title, str(raw.pub_time))
        if not dry_run and self.store:
            state = self.store.state_for(raw.source, raw.source_id)
            if (
                state
                and not self.force
                and state["source_hash"] == src_hash
                and state["status"] == "ok"
                and state["embed_model"] == config.EMBED_MODEL
            ):
                raise SkipDoc("unchanged")

        body = raw.content
        from_pdf = False
        if not trust_body:
            if self._needs_pdf(raw):
                pdf_text = self._load_pdf_text(raw)
                if pdf_text:
                    body = pdf_text
                    from_pdf = True
            body = clean.normalize(body)
            if from_pdf:
                body = clean.clean_pdf_noise(body)
            body = clean.drop_financial_statements(body)
            body = clean.filter_numeric_lines(body)
            body = clean.cut_boilerplate_tail(body)
            if raw.source_type in ("announcement",) + PERIODIC_TYPES:
                body = clean.strip_tail_signature(body)
            body = clean.normalize(body)
        else:
            body = clean.normalize(body)

        if raw.source_type in ("news", "news_flash"):
            org = clean.policy_org(raw.title, body)
            if org:
                raw.source_type = "industry_policy"
                raw.extra["policy_org"] = org
                raw.event_type = "政策"

        if not body.strip():
            raise SkipDoc("empty_body")
        if not raw.title:
            raw.title = clean.guess_title(body)

        score, reasons = quality.score_quality(raw.title, body, raw.source_type, raw.trust)
        if not quality.passes_gate(score, reasons, config.MIN_QUALITY):
            raise SkipDoc(f"low_quality({score:.2f};{','.join(reasons)})")

        fingerprint = quality.content_fingerprint(raw.title, raw.stock_codes, to_day(raw.pub_time), body)
        if not dry_run and self.store and not self.force:
            if self.store.fingerprint_exists(fingerprint):
                raise SkipDoc("duplicate_fingerprint")
            simhash = quality.simhash64(f"{raw.title}\n{body}")
            bands = quality.simhash_bands(simhash)
            if self.store.find_near_dup(simhash, bands, len(body.encode("utf-8")), raw.source_type):
                raise SkipDoc("near_duplicate")
        else:
            simhash = quality.simhash64(f"{raw.title}\n{body}")
            bands = quality.simhash_bands(simhash)

        return Prepared(
            raw=raw,
            body=body,
            fingerprint=fingerprint,
            simhash=simhash,
            bands=bands,
            quality=score,
            source_hash=src_hash,
            from_pdf=from_pdf,
        )

    # ------------------------------------------------------------------ 正文
    def _needs_pdf(self, raw: RawDoc) -> bool:
        if not raw.pdf_url:
            return False
        if raw.source_type in PERIODIC_TYPES:
            return True
        if raw.source_type == "research_report":
            return config.FETCH_REPORT_PDF
        if raw.source_type == "announcement":
            mode = config.ANNOUNCEMENT_PDF_MODE
            if mode == "none":
                return False
            if mode == "all":
                return True
            return clean.is_important_announcement(raw.title)
        return False

    def _load_pdf_text(self, raw: RawDoc) -> str:
        if raw.source_type not in PERIODIC_TYPES:
            if self._pdf_left <= 0:
                if raw.source_type == "research_report":
                    raise SkipDoc("pdf_budget_exhausted")
                return ""
            self._pdf_left -= 1

        try:
            path = download_pdf(raw.pdf_url, Path(config.PDF_CACHE_DIR), self.http)
        except Exception as exc:
            logger.warning("PDF 下载失败（%s）：%s", raw.title[:40], exc)
            if raw.source_type in PERIODIC_TYPES:
                raise SkipDoc(f"pdf_download_failed({type(exc).__name__})") from exc
            return ""

        try:
            from loaders import load_document  # ingest 根目录在 sys.path
            text, _ = load_document(str(path))
            return text or ""
        except Exception as exc:
            logger.warning("PDF 解析失败（%s）：%s", raw.title[:40], exc)
            if raw.source_type in PERIODIC_TYPES:
                raise SkipDoc(f"pdf_parse_failed({type(exc).__name__})") from exc
            return ""
        finally:
            if not config.KEEP_PDF:
                path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ 杂项
    def _chunk_metadata(self, prepared: Prepared, chunk: Chunk, total: int) -> dict:
        raw = prepared.raw
        return {
            "source": raw.source,
            "source_id": raw.source_id,
            "source_type": raw.source_type,
            "title": raw.title,
            "url": raw.url,
            "pub_time": raw.pub_time.isoformat() if raw.pub_time else None,
            "pub_date": to_day(raw.pub_time),
            "stock_codes": raw.stock_codes,
            "stock_names": raw.stock_names,
            "industry": raw.industry,
            "org": raw.org,
            "author": raw.author,
            "rating": raw.rating,
            "event_type": raw.event_type,
            "section": chunk.section,
            "chunk_seq": chunk.seq,
            "chunk_total": total,
            "quality_score": prepared.quality,
            "fingerprint": prepared.fingerprint,
            "fetched_at": now_cn().isoformat(),
            "extra": raw.extra,
        }

    def _advance_watermark(self, source, until, stats: RunStats, error: str | None, dry_run: bool) -> None:
        if dry_run:
            return
        # 大面积失败才停水位：单条坏 PDF 不该让整个源永久卡住；
        # 小比例失败由重叠窗口兜底，重复项有指纹挡着
        mass_failure = stats.failed > 0 and stats.failed >= max(3, stats.fetched * 0.2)
        if error or mass_failure:
            self.store.set_watermark(source.name, None, ok=False,
                                     error=error or f"{stats.failed}/{stats.fetched} docs failed")
            return
        if stats.failed:
            logger.warning("源 %s 有 %d 条失败，水位照常推进；下次窗口重叠会重试",
                           source.name, stats.failed)
        watermark = until if source.watermark_mode == "window" else stats.last_pub
        self.store.set_watermark(source.name, watermark, ok=True)

    @staticmethod
    def _append_spool(path: Path, prepared: Prepared) -> None:
        payload = asdict(prepared.raw)
        payload["content"] = prepared.body
        payload["pub_time"] = prepared.raw.pub_time.isoformat() if prepared.raw.pub_time else None
        payload["extra"] = {**prepared.raw.extra, "_prepared": True, "_quality": prepared.quality}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def iter_spool(path: Path):
    from ashare.util import parse_datetime

    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            payload["pub_time"] = parse_datetime(payload.get("pub_time"))
            yield RawDoc(**payload)
