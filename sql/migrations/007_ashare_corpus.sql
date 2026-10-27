-- A股文本语料：文档级富元数据 + 采集水位 + 运行审计
--
-- 向量仍只存一份（chunks.embedding），本迁移只加「语料特有的文档层信息」，
-- 因此 agent/rag/retriever.py 的检索链路无需改动。chunks.metadata 里另存完整
-- metadata 副本，用于按股票/来源/时间过滤；ashare_documents 承担增量、去重与统计。
--
-- 不在 documents 上加列：documents 同时被 ingest 文件管线使用，加列/加索引会
-- 把两套语义绑死，后续任何一边改字段都要动同一张表。

CREATE TABLE IF NOT EXISTS ashare_documents (
  doc_id        UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  source        TEXT NOT NULL,              -- 采集器：cninfo / cls / eastmoney / eastmoney-report
  source_id     TEXT NOT NULL,              -- 源侧主键：公告 announcementId、研报 infoCode 等
  source_type   TEXT NOT NULL,              -- annual_report / announcement / research_report / ...
  title         TEXT NOT NULL,
  url           TEXT,
  pub_time      TIMESTAMPTZ,
  stock_codes   TEXT[] NOT NULL DEFAULT '{}',
  stock_names   TEXT[] NOT NULL DEFAULT '{}',
  industry      TEXT,
  org           TEXT,                       -- 券商/研究机构
  author        TEXT,                       -- 分析师
  rating        TEXT,
  event_type    TEXT,                       -- 公告类型或研报类型
  -- 0~1：长度、中文占比、模板文本占比、来源可信度的加权结果，低于门禁不入库
  quality_score REAL NOT NULL DEFAULT 0,
  -- 去掉来源差异后的内容指纹：同一公告被两家源收录时指纹相同，用于跨源去重
  fingerprint   TEXT NOT NULL,
  simhash       BIGINT NOT NULL,            -- 近重复检测（64bit，按有符号存）
  simhash_bands INTEGER[] NOT NULL DEFAULT '{}',  -- 4×16bit 分段，GIN 索引做候选召回
  content_bytes INTEGER NOT NULL DEFAULT 0,
  -- 标题+时间+source_id 的哈希：命中即可跳过 PDF 下载与解析
  source_hash   TEXT NOT NULL DEFAULT '',
  extra         JSONB NOT NULL DEFAULT '{}'::jsonb,
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ashare_docs_fingerprint ON ashare_documents (fingerprint);
CREATE INDEX IF NOT EXISTS ashare_docs_pub_time ON ashare_documents (pub_time DESC);
CREATE INDEX IF NOT EXISTS ashare_docs_type_time ON ashare_documents (source_type, pub_time DESC);
CREATE INDEX IF NOT EXISTS ashare_docs_codes ON ashare_documents USING gin (stock_codes);
CREATE INDEX IF NOT EXISTS ashare_docs_simhash_bands ON ashare_documents USING gin (simhash_bands);
CREATE INDEX IF NOT EXISTS ashare_docs_source_hash ON ashare_documents (source, source_hash);

-- 采集水位：每个源一行。scope 预留给「按股票分片」的增量（如个股新闻），当前用空串
CREATE TABLE IF NOT EXISTS ashare_sync_state (
  source      TEXT NOT NULL,
  scope       TEXT NOT NULL DEFAULT '',
  watermark   TIMESTAMPTZ,
  cursor      JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_run_at TIMESTAMPTZ,
  last_ok_at  TIMESTAMPTZ,
  error       TEXT,
  PRIMARY KEY (source, scope)
);

-- 每次采集一批的执行留痕：失败率、入库量、耗时都能查，避免只看容器日志
CREATE TABLE IF NOT EXISTS ashare_ingest_runs (
  id          BIGSERIAL PRIMARY KEY,
  source      TEXT NOT NULL,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  fetched     INT NOT NULL DEFAULT 0,
  written     INT NOT NULL DEFAULT 0,
  skipped     INT NOT NULL DEFAULT 0,
  failed      INT NOT NULL DEFAULT 0,
  chunks      INT NOT NULL DEFAULT 0,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS ashare_runs_source_time ON ashare_ingest_runs (source, started_at DESC);

CREATE OR REPLACE VIEW ashare_corpus_stats AS
SELECT a.source,
       a.source_type,
       count(*)                        AS docs,
       COALESCE(sum(d.chunk_count), 0) AS chunks,
       min(a.pub_time)                 AS first_pub,
       max(a.pub_time)                 AS last_pub,
       round(avg(a.quality_score)::numeric, 3) AS avg_quality
FROM ashare_documents a
JOIN documents d ON d.id = a.doc_id
GROUP BY a.source, a.source_type;
