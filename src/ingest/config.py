"""Ingest 服务全局配置。所有配置项均可通过环境变量覆盖。"""
import os
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/stock_agent")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
WATCH_DIR = os.getenv("WATCH_DIR", "./data/docs")
# 离线（ingest 写库）用的 embedding 模型：FlagEmbedding 本地加载与 OpenAI 兼容 API
# 两个分支共用这一个名字，避免出现「本地一个、API 另一个」。必须与 agent 的
# ONLINE_EMBED_MODEL 同模型同维度，否则写入与检索落在两个向量空间。
OFFLINE_EMBED_MODEL = os.getenv("OFFLINE_EMBED_MODEL", "Qwen/Qwen3-Embedding-4B")
# 输出维度：必须与 agent 端、数据库列 vector(N) 三者一致。
# Qwen3 系列支持 dimensions 降维；1536 是保住 HNSW 的上限内取值
# （pgvector 对 vector 类型的 HNSW 索引硬限 2000 维）
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "512"))  # tokens
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "64"))

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".md", ".txt", ".csv", ".xlsx"}

# ---- Embedding 降级方案（FlagEmbedding 不可用时走 OpenAI 兼容 API）----
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Redis embedding 缓存 TTL（秒），默认 7 天
EMBED_CACHE_TTL = int(os.getenv("EMBED_CACHE_TTL", str(7 * 24 * 3600)))

# 去重锁 TTL（秒）
LOCK_TTL = int(os.getenv("LOCK_TTL", "300"))

EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))

# ---- 原件淘汰（磁盘保护）----
# 原始 PDF 全市场一年 60~120GB，而库里的向量 + 解析副本每块仅 ~20KB。
# 开启后入库即淘汰原件，只保留解析出的 Markdown 供 reembed 重建。
DELETE_RAW_AFTER_INGEST = os.getenv("DELETE_RAW_AFTER_INGEST", "true").lower() in ("1", "true", "yes")
# 解析副本目录：默认与 WATCH_DIR 同级（本地 ./data/parsed，容器 /data/parsed），
# 必须在监听目录之外，否则副本会被当成新文档重新入库。
PARSED_DIR = os.getenv("PARSED_DIR", str(Path(WATCH_DIR).parent / "parsed"))
# 墓碑 TTL：要覆盖 watchdog 去抖(2s) + 删除回调排队，120s 足够
TOMBSTONE_TTL = int(os.getenv("TOMBSTONE_TTL", "120"))
