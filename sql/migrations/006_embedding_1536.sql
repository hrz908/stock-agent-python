-- embedding 维度 1024 -> 1536（已初始化库的增量迁移）
--
-- 背景：embedding 模型换成 Qwen/Qwen3-Embedding-4B，请求带 dimensions=1536。
-- 1536 <= 2000，vector 类型仍可建 HNSW；2048/2560 会报
-- "column cannot have more than 2000 dimensions for hnsw index"。
--
-- 注意：换维度 = 换向量空间，旧向量全部作废。本脚本只在 chunks 为空时可直接执行；
-- 若已有数据，先 TRUNCATE chunks 再跑，否则 pgvector 无法把 1024 维转成 1536 维。
--
-- 新库无需执行：005_agent.sql 已按 1536 建表，重复执行本脚本是幂等的（同维度 ALTER 无副作用）。

DROP INDEX IF EXISTS chunks_hnsw;

ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(1536);

CREATE INDEX chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 128);

-- documents.embed_model 与当前模型不一致的记录会被 ingest 的重嵌查询捞出来，
-- 这里不要动 status，否则会排除在 status='ok' 的筛选条件之外。
