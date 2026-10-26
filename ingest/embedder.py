"""向量嵌入器：支持 FlagEmbedding 与 OpenAI 兼容 API 降级。"""
import hashlib
import logging
import struct

import redis

from config import (
    EMBED_BATCH_SIZE,
    EMBED_CACHE_TTL,
    EMBED_DIM,
    OFFLINE_EMBED_MODEL,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    REDIS_URL,
)

logger = logging.getLogger(__name__)


def _cache_key(model: str, text: str) -> str:
    """Redis 缓存 key: emb:{model}:{dim}:{content_hash}

    维度必须进 key：同一模型可用 dimensions 输出不同维度，
    不隔离会命中错维度缓存，写 pgvector 时维度校验直接失败。
    """
    h = hashlib.sha256(text.encode()).hexdigest()[:16]
    return f"emb:{model}:{EMBED_DIM}:{h}"


def _serialize_vector(vec: list[float]) -> bytes:
    """将 float 向量序列化为 bytes 存入 Redis"""
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_vector(data: bytes) -> list[float]:
    """从 Redis bytes 反序列化向量"""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


class Embedder:
    """
    向量嵌入器。
    
    优先使用 FlagEmbedding (BGEM3FlagModel)，加载失败时降级到
    OpenAI 兼容的 embedding API（通过 OPENAI_API_BASE 调用）。
    """

    def __init__(self):
        self.model = None  # 延迟加载
        self._backend: str | None = None  # "flag" | "openai"
        self.redis: redis.Redis = redis.from_url(REDIS_URL, decode_responses=False)
        self._openai_client = None

    def _load_model(self):
        """延迟加载嵌入模型，FlagEmbedding 优先，失败降级 OpenAI"""
        if self._backend is not None:
            return

        # 尝试 FlagEmbedding
        try:
            from FlagEmbedding import BGEM3FlagModel
            logger.info("加载 FlagEmbedding 模型: %s", OFFLINE_EMBED_MODEL)
            self.model = BGEM3FlagModel(OFFLINE_EMBED_MODEL, use_fp16=True)
            self._backend = "flag"
            logger.info("FlagEmbedding 模型加载成功")
            return
        except ImportError:
            logger.warning("FlagEmbedding 未安装，尝试 OpenAI 降级方案")
        except Exception as e:
            logger.warning("FlagEmbedding 加载失败 (%s)，尝试 OpenAI 降级方案", e)

        # 降级: OpenAI 兼容 API
        try:
            import openai
            if OPENAI_API_BASE:
                self._openai_client = openai.OpenAI(
                    base_url=OPENAI_API_BASE,
                    api_key=OPENAI_API_KEY or "sk-placeholder",
                )
            else:
                self._openai_client = openai.OpenAI(
                    api_key=OPENAI_API_KEY,
                )
            self._backend = "openai"
            logger.info("使用 OpenAI 兼容 embedding API: model=%s, base=%s",
                        OFFLINE_EMBED_MODEL, OPENAI_API_BASE or "(default)")
        except ImportError as exc:
            raise RuntimeError(
                "无法初始化嵌入模型：FlagEmbedding 和 openai 库均不可用。"
                "请安装 FlagEmbedding 或 openai 并配置 OPENAI_API_BASE / OPENAI_API_KEY。"
            ) from exc
        except Exception as e:
            raise RuntimeError(f"OpenAI embedding 客户端初始化失败: {e}") from e

    def _embed_flag(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """使用 FlagEmbedding 批量嵌入"""
        output = self.model.encode(
            texts,
            batch_size=EMBED_BATCH_SIZE,
            max_length=8192,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        # output["dense_vecs"] shape: (n, dim)
        vecs = output["dense_vecs"]
        return [v.tolist() for v in vecs]

    def _embed_openai(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """使用 OpenAI 兼容 API 批量嵌入"""
        all_vecs = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i:i + EMBED_BATCH_SIZE]
            resp = self._openai_client.embeddings.create(
                model=OFFLINE_EMBED_MODEL,
                input=batch,
                dimensions=EMBED_DIM,
            )
            batch_vecs = [item.embedding for item in resp.data]
            all_vecs.extend(batch_vecs)
        return all_vecs

    def embed(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """
        批量嵌入文本，带 Redis 缓存。

        Args:
            texts: 待嵌入文本列表
            is_query: 是否为查询（部分模型区分 query/doc）

        Returns:
            嵌入向量列表（维度由 EMBED_DIM 决定，Qwen3-Embedding-4B 默认 2560，
            本项目固定 1536 以保住 HNSW 索引）
        """
        if not texts:
            return []

        self._load_model()
        model_name = OFFLINE_EMBED_MODEL

        cached: list[list[float] | None] = [None] * len(texts)
        miss_indices: list[int] = []
        miss_texts: list[str] = []

        for i, text in enumerate(texts):
            key = _cache_key(model_name, text)
            try:
                data = self.redis.get(key)
                if data is not None:
                    cached[i] = _deserialize_vector(data)
                else:
                    miss_indices.append(i)
                    miss_texts.append(text)
            except Exception:
                # Redis 故障不阻塞，直接当 miss
                miss_indices.append(i)
                miss_texts.append(text)

        logger.debug("嵌入缓存命中 %d/%d，miss %d",
                     len(texts) - len(miss_texts), len(texts), len(miss_texts))

        if miss_texts:
            if self._backend == "flag":
                new_vecs = self._embed_flag(miss_texts, is_query)
            else:
                new_vecs = self._embed_openai(miss_texts, is_query)

            for idx, vec in zip(miss_indices, new_vecs, strict=True):
                cached[idx] = vec
                key = _cache_key(model_name, texts[idx])
                try:
                    self.redis.setex(key, EMBED_CACHE_TTL, _serialize_vector(vec))
                except Exception as e:
                    logger.debug("写入 embedding 缓存失败: %s", e)

        result = [v for v in cached if v is not None]
        if len(result) != len(texts):
            logger.warning("嵌入结果数量不匹配: expected %d, got %d", len(texts), len(result))
        return result
