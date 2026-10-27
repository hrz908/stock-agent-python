"""完整处理流水线：加载 -> 分块 -> 嵌入 -> 入库 -> 淘汰原件。"""
import logging
import os
from pathlib import Path

import redis

from ingest.chunker import chunk_markdown
from ingest.config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    DELETE_RAW_AFTER_INGEST,
    LOCK_TTL,
    OFFLINE_EMBED_MODEL,
    PARSED_DIR,
    TOMBSTONE_TTL,
)
from ingest.embedder import Embedder
from ingest.loaders import content_hash, load_document
from ingest.store import Store

logger = logging.getLogger(__name__)


def tombstone_key(path: str) -> str:
    """原件淘汰标记：让删除回调区分「ingest 自己删」与「用户删」。

    key 走绝对路径，因为 watcher 事件与 process_file 入参可能一绝对一相对。
    """
    return f"tomb:ingest:{Path(path).resolve()}"


def parsed_path(doc_hash: str) -> Path:
    """解析副本路径，按 content_hash 命名：原件淘汰后 reembed 只认它。"""
    return Path(PARSED_DIR) / f"{doc_hash}.md"


def _write_parsed(content: str, doc_hash: str) -> Path:
    """落解析副本：先写临时文件再 replace，避免 reembed 读到半截文件。

    临时名带 pid：同内容不同路径的文档可能并发处理（锁是按路径加的）。
    """
    target = parsed_path(doc_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.{os.getpid()}.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)
    return target


def _retire_raw_file(
    path: str,
    content: str,
    doc_hash: str,
    redis_client: redis.Redis,
) -> bool:
    """淘汰原件：先落副本、再打墓碑、最后删文件。

    顺序不可调换——副本或墓碑任一步失败都保留原件，否则会出现
    「文件没了 -> watcher 删库 -> 向量凭空消失」。
    """
    try:
        parsed = _write_parsed(content, doc_hash)
    except OSError as e:
        logger.error("解析副本写入失败，保留原件: %s - %s", path, e)
        return False

    try:
        redis_client.setex(tombstone_key(path), TOMBSTONE_TTL, "1")
    except Exception as e:
        logger.error("墓碑写入失败，保留原件（否则删除回调会级联删库）: %s - %s", path, e)
        return False

    try:
        Path(path).unlink()
    except OSError as e:
        logger.warning("删除原件失败（副本已存在，不影响检索）: %s - %s", path, e)
        return False

    logger.info("已淘汰原件并保留解析副本: %s -> %s", path, parsed)
    return True


async def _ingest(
    content: str,
    source_path: str,
    mime_type: str,
    store: Store,
    embedder: Embedder,
) -> tuple[str, str]:
    """正文 -> 分块 -> 嵌入 -> 入库，返回 (状态, content_hash)。

    状态三态：written（新写入）/ unchanged（幂等跳过，库里已有）/ failed（没进库）。
    淘汰原件必须区分 failed 与 unchanged——嵌入失败时删了 PDF 等于把文档从知识库抹掉。
    """
    doc_hash = content_hash(content)
    title = Path(source_path).stem

    chunks = chunk_markdown(content, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
    if not chunks:
        logger.warning("分块结果为空，跳过: %s", source_path)
        return "failed", doc_hash

    texts = [c["content"] for c in chunks]
    try:
        embeddings = embedder.embed(texts)
    except Exception as e:
        logger.error("嵌入失败: %s - %s", source_path, e)
        return "failed", doc_hash

    for chunk, emb in zip(chunks, embeddings, strict=True):
        chunk["embedding"] = emb

    doc_id = await store.transactional_upsert(
        source_path=source_path,
        title=title,
        mime_type=mime_type,
        content_hash=doc_hash,
        embed_model=OFFLINE_EMBED_MODEL,
        chunks=chunks,
    )
    if doc_id is None:
        logger.info("文档无变化，已跳过: %s", source_path)
        return "unchanged", doc_hash

    logger.info("文件入库成功: %s -> doc_id=%s, chunks=%d", source_path, doc_id, len(chunks))
    return "written", doc_hash


async def process_file(
    path: str,
    store: Store,
    embedder: Embedder,
    redis_client: redis.Redis,
) -> bool:
    """处理单个文件，返回是否实际入库（False 表示跳过）。

    流程：Redis 去重锁 -> 加载 -> 分块 -> 嵌入 -> 单事务写入 -> 淘汰原件 -> 释放锁；
    transactional_upsert 返回 None 表示内容无变化（幂等跳过）。
    """
    p = Path(path)
    if not p.exists():
        logger.warning("文件不存在，跳过: %s", path)
        return False

    if not p.is_file():
        logger.debug("非文件路径，跳过: %s", path)
        return False

    lock_key = f"lock:ingest:{path}"
    try:
        acquired = redis_client.set(lock_key, "1", nx=True, ex=LOCK_TTL)
        if not acquired:
            logger.info("文件正在被其他进程处理，跳过: %s", path)
            return False
    except Exception as e:
        logger.warning("Redis 锁获取异常 (%s)，继续处理: %s", e, path)

    try:
        logger.info("开始处理文件: %s", path)
        try:
            content, mime_type = load_document(str(p))
        except Exception as e:
            logger.error("文档加载失败: %s - %s", path, e)
            return False

        if not content or not content.strip():
            logger.warning("文档内容为空，跳过: %s", path)
            return False

        status, doc_hash = await _ingest(content, str(p), mime_type, store, embedder)
        if status == "failed":
            logger.warning("入库失败，保留原件以便重试: %s", path)
            return False

        # 淘汰原件：unchanged 也要淘汰，否则重复扫描的原件会一直占盘
        if DELETE_RAW_AFTER_INGEST:
            _retire_raw_file(str(p), content, doc_hash, redis_client)

        return status == "written"

    except Exception:
        logger.exception("处理文件异常: %s", path)
        return False

    finally:
        try:
            redis_client.delete(lock_key)
        except Exception as e:
            logger.debug("释放锁失败: %s", e)


async def reembed_document(
    doc: dict,
    store: Store,
    embedder: Embedder,
) -> bool:
    """重建单个文档的向量：源文件在就读源文件，不在就读解析副本。

    原件默认已被淘汰，所以必须先找副本；两者都没有才认定文档真的没了。
    """
    source_path = doc["source_path"]
    if source_path.startswith("ashare://"):
        # A股语料没有本地原件，重嵌由 ashare.cli reembed 负责；这里若继续会删库
        logger.info("跳过 A股语料（交给 ashare reembed）：%s", source_path)
        return False

    p = Path(source_path)

    if p.exists():
        content, mime_type = load_document(str(p))
    else:
        parsed = parsed_path(doc["content_hash"])
        if not parsed.exists():
            logger.warning("源文件与解析副本均不存在，清理记录: %s", source_path)
            await store.delete_document_by_path(source_path)
            return False
        content = parsed.read_text(encoding="utf-8")
        mime_type = doc.get("mime_type") or "text/markdown"
        logger.info("原件已淘汰，改用解析副本重建: %s", parsed)

    status, _ = await _ingest(content, source_path, mime_type, store, embedder)
    return status == "written"


async def delete_file(path: str, store: Store, redis_client: redis.Redis) -> bool:
    """源文件被删除时清理库记录。

    ingest 自己淘汰的原件带墓碑标记，必须跳过——否则刚入库的向量会被自己的
    清理动作删掉。Redis 不可用时无法判定来源，同样选择不删（宁可留脏记录）。
    """
    try:
        if redis_client.get(tombstone_key(path)):
            logger.info("原件已由 ingest 淘汰（解析副本保留），跳过删库: %s", path)
            return False
    except Exception as e:
        logger.warning("墓碑查询失败，跳过删库以保护记录: %s - %s", path, e)
        return False

    try:
        await store.delete_document_by_path(path)
        logger.info("已清理删除文件的数据库记录: %s", path)
        return True
    except Exception as e:
        logger.error("删除文件记录失败: %s - %s", path, e)
        return False
