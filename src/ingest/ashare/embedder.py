"""向量化：本地 sentence-transformers 优先，其次 FlagEmbedding / OpenAI 兼容 API。

维度是硬约束：写库前必须与 chunks.embedding 的 vector(N) 一致。Qwen3 系列支持
MRL 截断，原生 2560 维可在本地截到 1536 并重新归一化，不必上 vLLM。
"""
import logging
import math
import time

from ingest.ashare import config

logger = logging.getLogger(__name__)

# Qwen3-Embedding 的官方查询指令；只用于检索端，入库端不加
QUERY_INSTRUCTION = (
    "Instruct: Given a Chinese financial question or keyword, retrieve relevant passages "
    "that answer the question\nQuery: "
)


class Embedder:
    def __init__(self):
        self._backend: str | None = None
        self._model = None
        self._client = None
        self._native_dim: int | None = None

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        if self._backend is not None:
            return

        if config.EMBED_API_BASE:
            import openai

            self._client = openai.OpenAI(
                base_url=config.EMBED_API_BASE,
                api_key=config.EMBED_API_KEY or "sk-local",
            )
            self._backend = "api"
            logger.info("embedding 后端：API %s (%s)", config.EMBED_API_BASE, config.EMBED_MODEL)
            return

        model_ref = config.EMBED_MODEL_PATH or config.EMBED_MODEL
        try:
            from sentence_transformers import SentenceTransformer

            kwargs = {"trust_remote_code": True}
            if config.DEVICE and config.DEVICE != "auto":
                kwargs["device"] = config.DEVICE
            self._model = SentenceTransformer(model_ref, **kwargs)
            self._model.max_seq_length = config.EMBED_MAX_SEQ
            self._native_dim = int(self._model.get_sentence_embedding_dimension())
            self._backend = "st"
            logger.info("embedding 后端：sentence-transformers %s（原生 %d 维，device=%s）",
                        model_ref, self._native_dim, config.DEVICE)
            return
        except ImportError:
            logger.warning("sentence-transformers 未安装，尝试 FlagEmbedding")
        except Exception as exc:
            logger.warning("sentence-transformers 加载失败（%s），尝试 FlagEmbedding", exc)

        try:
            from FlagEmbedding import BGEM3FlagModel

            self._model = BGEM3FlagModel(model_ref, use_fp16=config.DEVICE != "cpu")
            self._backend = "flag"
            logger.info("embedding 后端：FlagEmbedding %s", model_ref)
            return
        except ImportError as exc:
            raise RuntimeError(
                "没有可用的 embedding 后端：安装 requirements-embed.txt，"
                "或设置 ASHARE_EMBED_API_BASE 指向 OpenAI 兼容服务"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"FlagEmbedding 加载失败：{exc}") from exc

    # ----------------------------------------------------------------- encode
    def _fit_dim(self, vector: list[float]) -> list[float]:
        dim = config.EMBED_DIM
        native = len(vector)
        if native == dim:
            return vector
        if native > dim and config.EMBED_TRUNCATE:
            # MRL 截断后必须重新归一化，否则余弦相似度尺度会漂
            head = vector[:dim]
            norm = math.sqrt(sum(x * x for x in head)) or 1.0
            return [x / norm for x in head]
        raise RuntimeError(
            f"向量维度不匹配：模型输出 {native}，配置 ASHARE_EMBED_DIM={dim}，"
            f"数据库 vector 列也必须是 {dim}。换模型需同时改三处并重建索引。"
        )

    def _encode_local(self, texts: list[str], is_query: bool) -> list[list[float]]:
        batch = [QUERY_INSTRUCTION + t for t in texts] if is_query else texts
        if self._backend == "st":
            vectors = self._model.encode(
                batch,
                batch_size=config.EMBED_BATCH,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return [self._fit_dim(v.tolist()) for v in vectors]

        output = self._model.encode(
            batch,
            batch_size=config.EMBED_BATCH,
            max_length=config.EMBED_MAX_SEQ,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        return [self._fit_dim(v.tolist()) for v in output["dense_vecs"]]

    def _encode_api(self, texts: list[str], is_query: bool) -> list[list[float]]:
        batch = [QUERY_INSTRUCTION + t for t in texts] if is_query else texts
        try:
            resp = self._client.embeddings.create(
                model=config.EMBED_MODEL, input=batch, dimensions=config.EMBED_DIM
            )
        except Exception as exc:
            # TEI 等后端不接受 dimensions 参数，退一步按原生维度取，再走截断
            logger.debug("带 dimensions 的请求失败（%s），改按原生维度请求", exc)
            resp = self._client.embeddings.create(model=config.EMBED_MODEL, input=batch)
        return [self._fit_dim(item.embedding) for item in resp.data]

    # ----------------------------------------------------------------- public
    def embed(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """批量嵌入；同一批内完全相同的文本只算一次。"""
        if not texts:
            return []
        self._load()

        unique = list(dict.fromkeys(texts))
        results: dict[str, list[float]] = {}
        for start in range(0, len(unique), config.EMBED_BATCH):
            batch = unique[start:start + config.EMBED_BATCH]
            last_error: Exception | None = None
            for attempt in range(config.EMBED_RETRIES + 1):
                try:
                    if self._backend == "api":
                        vectors = self._encode_api(batch, is_query)
                    else:
                        vectors = self._encode_local(batch, is_query)
                    break
                except Exception as exc:
                    last_error = exc
                    logger.warning("嵌入失败(%s/%s)：%s", attempt + 1, config.EMBED_RETRIES + 1, exc)
                    time.sleep(1.5 * (attempt + 1))
            else:
                raise RuntimeError(f"嵌入连续失败：{last_error}")
            results.update(zip(batch, vectors, strict=True))

        return [results[t] for t in texts]
