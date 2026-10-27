-- 中文全文检索：应用侧 bigram 词元 + tsv_zh GIN 索引
--
-- 背景：chunks.tsv 使用 to_tsvector('simple', content)，PostgreSQL 不会切分连续中文，
-- 查询侧 websearch_to_tsquery('simple', 原始问题) 基本无法命中，lex 分支形同虚设。
-- 方案：ingest 写入 search_text（中文 bigram / ASCII 词），查询侧用同一套切分拼
-- to_tsquery；tsv_zh 由 search_text 生成，避免在数据库里做中文分词。
--
-- 注意：本迁移只建列和索引。已有 chunks 的 search_text 仍为空，需要重新 ingest
-- 或跑离线 backfill；新库/新语料不受影响。

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS search_text TEXT NOT NULL DEFAULT '';

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv_zh tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED;

CREATE INDEX IF NOT EXISTS chunks_tsv_zh ON chunks USING gin (tsv_zh);
