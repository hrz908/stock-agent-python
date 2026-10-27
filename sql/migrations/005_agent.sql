-- Agent 相关表：文档知识库 + 向量检索 + 运行审计
--
-- 依赖扩展：
--   vector   —— pgvector，用于 embedding 列 + HNSW 近似最近邻索引
--   pg_trgm  —— 三元组索引，用于模糊匹配/相似度检索兜底
--
-- 说明：
--   tsv 使用 'simple' 配置（本地镜像未装 zhparser，中文分词交由上层处理）
--   embedding 维度固定 1536（Qwen3-Embedding-4B + dimensions=1536）；
--   上限卡在 2000：pgvector 对 vector 类型的 HNSW 索引超 2000 维直接拒绝，
--   故 2048/2560 都要么删索引、要么换 halfvec，这里取 1536 保住 HNSW
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 文档表：一条记录 = 一份被 ingest 的原始文档
CREATE TABLE documents (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_path  TEXT UNIQUE NOT NULL,
  title        TEXT,
  mime_type    TEXT,
  content_hash TEXT NOT NULL,
  embed_model  TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'ok',
  chunk_count  INT NOT NULL DEFAULT 0,
  error        TEXT,
  updated_at   TIMESTAMPTZ DEFAULT now()
);

-- 分块表：文档切片 + 向量 + 全文检索列
CREATE TABLE chunks (
  id          BIGSERIAL PRIMARY KEY,
  document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
  seq         INT NOT NULL,
  content     TEXT NOT NULL,
  token_count INT,
  metadata    JSONB DEFAULT '{}'::jsonb,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
  embedding   vector(1536) NOT NULL,
  UNIQUE (document_id, seq)
);

-- 索引：
--   HNSW  —— 向量近邻检索主索引
--   GIN   —— 全文检索 (tsv) 与 JSONB 元数据过滤
--   btree —— 按 embedding 模型筛选文档（便于换模型时批量重建）
CREATE INDEX chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 128);
CREATE INDEX chunks_tsv  ON chunks USING gin (tsv);
CREATE INDEX chunks_meta ON chunks USING gin (metadata jsonb_path_ops);
CREATE INDEX docs_model  ON documents (embed_model);

-- Agent 运行审计表：一次 LLM 调用/一轮对话执行留痕，便于观测与复盘
CREATE TABLE agent_runs (
  run_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     BIGINT NOT NULL,
  thread_id   TEXT NOT NULL,
  intent      TEXT,
  confidence  REAL,
  tokens_in   INT DEFAULT 0,
  tokens_out  INT DEFAULT 0,
  ttft_ms     INT,
  total_ms    INT,
  tool_calls  INT DEFAULT 0,
  error_code  TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX agent_runs_user   ON agent_runs (user_id, created_at DESC);
CREATE INDEX agent_runs_thread ON agent_runs (thread_id);
