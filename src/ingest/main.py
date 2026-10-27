"""Ingest 服务 CLI 入口。"""
import asyncio
import logging
import sys
from pathlib import Path

import redis

from ingest.config import (
    DATABASE_URL,
    OFFLINE_EMBED_MODEL,
    REDIS_URL,
    SUPPORTED_EXTENSIONS,
    WATCH_DIR,
)
from ingest.embedder import Embedder
from ingest.pipeline import delete_file, process_file, reembed_document
from ingest.store import Store
from ingest.watcher import start_watcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ingest")


def _get_redis() -> redis.Redis:
    return redis.from_url(REDIS_URL, decode_responses=True)


def _find_files(watch_dir: str) -> list[str]:
    """递归扫描目录，找到所有支持的文件"""
    base = Path(watch_dir)
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        logger.info("监听目录不存在，已创建: %s", watch_dir)
        return []

    files = []
    for p in base.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(str(p))
    return sorted(files)


# ------------------------------------------------------------------
# 子命令
# ------------------------------------------------------------------

async def cmd_scan():
    """全量扫描 WATCH_DIR"""
    logger.info("=== 全量扫描模式 ===")
    logger.info("监听目录: %s", WATCH_DIR)

    store = Store(DATABASE_URL)
    await store.connect()
    await store.ensure_tables()

    embedder = Embedder()
    redis_client = _get_redis()

    files = _find_files(WATCH_DIR)
    logger.info("发现 %d 个支持的文件", len(files))

    success_count = 0
    skip_count = 0
    fail_count = 0

    for f in files:
        try:
            result = await process_file(f, store, embedder, redis_client)
            if result:
                success_count += 1
            else:
                skip_count += 1
        except Exception as e:
            logger.error("处理失败: %s - %s", f, e)
            fail_count += 1

    logger.info("扫描完成: 成功=%d, 跳过=%d, 失败=%d", success_count, skip_count, fail_count)
    await store.close()


async def cmd_watch():
    """常驻监听模式"""
    logger.info("=== 常驻监听模式 ===")
    logger.info("监听目录: %s", WATCH_DIR)

    store = Store(DATABASE_URL)
    await store.connect()
    await store.ensure_tables()

    embedder = Embedder()
    redis_client = _get_redis()

    loop = asyncio.get_running_loop()

    async def on_process(path: str):
        await process_file(path, store, embedder, redis_client)

    async def on_delete(path: str):
        await delete_file(path, store, redis_client)

    observer = start_watcher(WATCH_DIR, loop, on_process, on_delete)

    # 启动后先做一次全量扫描
    files = _find_files(WATCH_DIR)
    if files:
        logger.info("启动时全量扫描: %d 个文件", len(files))
        for f in files:
            try:
                await process_file(f, store, embedder, redis_client)
            except Exception as e:
                logger.error("扫描处理失败: %s - %s", f, e)

    logger.info("监听中... 按 Ctrl+C 退出")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("收到退出信号，正在关闭...")
    finally:
        observer.stop()
        observer.join(timeout=5)
        await store.close()
        logger.info("服务已停止")


async def cmd_once(path: str):
    """单文件调试模式"""
    logger.info("=== 单文件处理模式 ===")
    logger.info("文件路径: %s", path)

    p = Path(path)
    if not p.exists():
        logger.error("文件不存在: %s", path)
        sys.exit(1)

    store = Store(DATABASE_URL)
    await store.connect()
    await store.ensure_tables()

    embedder = Embedder()
    redis_client = _get_redis()

    result = await process_file(str(p.resolve()), store, embedder, redis_client)
    if result:
        logger.info("处理成功: %s", path)
    else:
        logger.info("已跳过（无变化或处理失败）: %s", path)

    await store.close()


async def cmd_reembed(model: str):
    """按 embed_model 找出陈旧文档全量重灌"""
    logger.info("=== Reembed 模式 ===")
    logger.info("当前模型: %s, 目标模型: %s", model, OFFLINE_EMBED_MODEL)

    store = Store(DATABASE_URL)
    await store.connect()
    await store.ensure_tables()

    embedder = Embedder()

    stale_docs = await store.get_stale_documents(model)
    logger.info("发现 %d 个陈旧文档需要重新嵌入", len(stale_docs))

    success_count = 0
    skip_count = 0
    fail_count = 0

    for doc in stale_docs:
        source_path = doc["source_path"]
        try:
            # 原件可能已被淘汰，reembed_document 内部会回落到解析副本
            if await reembed_document(doc, store, embedder):
                success_count += 1
                logger.info("重嵌入成功: %s", source_path)
            else:
                skip_count += 1
                logger.info("跳过: %s", source_path)
        except Exception as e:
            logger.error("重嵌入失败: %s - %s", source_path, e)
            fail_count += 1

    logger.info("Reembed 完成: 成功=%d, 跳过=%d, 失败=%d", success_count, skip_count, fail_count)
    await store.close()


# ------------------------------------------------------------------
# CLI 解析
# ------------------------------------------------------------------

def print_usage():
    print("""
Ingest 文档入库服务

用法:
  python main.py scan                  全量扫描 WATCH_DIR
  python main.py watch                 常驻监听模式
  python main.py once <path>           单文件处理（调试）
  python main.py reembed --model <m>   重新嵌入指定模型的陈旧文档

环境变量:
  DATABASE_URL    PostgreSQL 连接串
  REDIS_URL       Redis 连接串
  WATCH_DIR       监听目录 (默认: ./data/docs)
  OFFLINE_EMBED_MODEL  离线嵌入模型 (默认: Qwen/Qwen3-Embedding-4B)，须与 agent 的
                       ONLINE_EMBED_MODEL 同模型同维度
  EMBED_DIM       输出维度 (默认: 1536，须与数据库 vector(N) 一致)
  CHUNK_SIZE      分块大小 tokens (默认: 512)
  CHUNK_OVERLAP   分块重叠 tokens (默认: 64)
  DELETE_RAW_AFTER_INGEST  入库后淘汰原件 (默认: true，设 0 保留)
  PARSED_DIR      解析副本目录 (默认: WATCH_DIR 同级 /parsed)，原件淘汰后 reembed 靠它
""")


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "scan":
        asyncio.run(cmd_scan())
    elif cmd == "watch":
        asyncio.run(cmd_watch())
    elif cmd == "once":
        if len(sys.argv) < 3:
            print("错误: once 命令需要指定文件路径")
            print("用法: python main.py once <path>")
            sys.exit(1)
        asyncio.run(cmd_once(sys.argv[2]))
    elif cmd == "reembed":
        model = OFFLINE_EMBED_MODEL
        if "--model" in sys.argv:
            idx = sys.argv.index("--model")
            if idx + 1 < len(sys.argv):
                model = sys.argv[idx + 1]
        asyncio.run(cmd_reembed(model))
    else:
        print(f"未知命令: {cmd}")
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
