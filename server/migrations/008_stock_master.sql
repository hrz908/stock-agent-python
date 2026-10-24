-- A股主数据：股票身份 / 板块 / 行业 / 概念
--
-- 主键取 6 位裸码而不是 ts_code：RAG 的 ashare_documents.stock_codes 和 agent 的全部
-- 取数工具都用裸码，只有 Go 侧 user_watchlist 存 SH600519；格式转换放在出口（见 v_stock_profile），
-- 库里只保留一种写法。
--
-- market / board 用生成列：Python 的 to_market 按首位判 920 为 sz、Go 的 MarketOf 判 BJ，
-- 两边口径已经分叉；写成一个 CASE 之后新增代码前缀只改一处。未知前缀取不到值，
-- 由 NOT NULL 直接拒绝入库——宁可报错，也不要悄悄落到错的交易所（B 股 900/200 就属于这类）。
--
-- 前缀表在本文件、server/internal/domain/stockcode/code.go、小程序 watchlist.js 各有一份，
-- 上游开新号段（如创业板 302）必须同步三处，漏一处就是入库失败或前端搜不到。
--
-- 估值类字段（total_mv / pe / pb / 股息率）不进本表：它们是每日变动的行情快照，
-- 网关已有 Redis 30s 缓存 + 腾讯实时源，落库即过时。

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS stock_basic (
  code        VARCHAR(6) PRIMARY KEY CHECK (code ~ '^[0-9]{6}$'),
  market      VARCHAR(2) GENERATED ALWAYS AS (
                CASE
                  WHEN left(code, 3) IN ('600', '601', '603', '605', '688', '689') THEN 'SH'
                  WHEN left(code, 3) IN ('000', '001', '002', '003', '300', '301', '302') THEN 'SZ'
                  WHEN left(code, 3) = '920' OR left(code, 2) IN ('43', '83', '87', '88') THEN 'BJ'
                END) STORED NOT NULL,
  board       VARCHAR(8) GENERATED ALWAYS AS (
                CASE
                  WHEN left(code, 3) IN ('688', '689') THEN 'star'
                  WHEN left(code, 3) IN ('300', '301', '302') THEN 'gem'
                  WHEN left(code, 3) = '920' OR left(code, 2) IN ('43', '83', '87', '88') THEN 'bse'
                  WHEN left(code, 3) IN ('600', '601', '603', '605', '000', '001', '002', '003') THEN 'main'
                END) STORED NOT NULL,
  name        VARCHAR(32) NOT NULL,
  -- 拼音只服务 symbol_search 的本地兜底（腾讯 smartbox 是主路），缺了不影响主流程
  pinyin      VARCHAR(32),
  pinyin_abbr VARCHAR(16),
  list_date   DATE,
  delist_date DATE,
  -- 只记上市/退市这类低频状态；停牌由行情接口实时给，不进表（否则要分钟级同步）
  status      VARCHAR(8) NOT NULL DEFAULT 'listed' CHECK (status IN ('listed', 'delisted')),
  -- 由名称前缀维护的派生标记：问答里「排除 ST」是高频过滤，不该每次靠 LIKE 猜
  is_st       BOOLEAN NOT NULL DEFAULT FALSE,
  source      VARCHAR(16) NOT NULL DEFAULT 'akshare',
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 名称不做唯一约束：更名、A/B 股并存都可能重名，模糊匹配比强约束更合适
CREATE INDEX IF NOT EXISTS stock_basic_name_trgm ON stock_basic USING gin (name gin_trgm_ops);
-- 拼音缩写走 ILIKE（gzmt / gz），trgm 索引对它有效；btree 的 text_pattern_ops 只服务 LIKE
CREATE INDEX IF NOT EXISTS stock_basic_pinyin_trgm ON stock_basic USING gin (pinyin_abbr gin_trgm_ops);
CREATE INDEX IF NOT EXISTS stock_basic_status ON stock_basic (status, is_st);

-- 行业：一只股票在一个分类标准下只有一条；em(东财) 与 sw(申万) 口径不同，不能混存一行
CREATE TABLE IF NOT EXISTS stock_industry (
  code       VARCHAR(6) NOT NULL REFERENCES stock_basic(code) ON DELETE CASCADE,
  std        VARCHAR(8) NOT NULL DEFAULT 'em',
  l1         VARCHAR(32) NOT NULL,
  l2         VARCHAR(32),
  l3         VARCHAR(32),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (code, std)
);

CREATE INDEX IF NOT EXISTS stock_industry_l1 ON stock_industry (std, l1, l2);

-- 概念：多对多。concept 名称是主键首列（「白酒概念有哪些股」比反向查询更常用），
-- 反查单只股票的概念由 stock_concept_code 支撑
CREATE TABLE IF NOT EXISTS stock_concept (
  concept    VARCHAR(48) NOT NULL,
  code       VARCHAR(6) NOT NULL REFERENCES stock_basic(code) ON DELETE CASCADE,
  board_code VARCHAR(16),
  source     VARCHAR(16) NOT NULL DEFAULT 'em',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (concept, code)
);

CREATE INDEX IF NOT EXISTS stock_concept_code ON stock_concept (code);

-- 统一出口：代码三种写法一次给全，调用方不再自己拼
--   ts_code  600519.SH（Tushare/LLM 上下文口径）
--   gw_code  SH600519 （Go 网关 / user_watchlist 口径）
CREATE OR REPLACE VIEW v_stock_profile AS
SELECT b.code,
       b.market,
       b.code || '.' || b.market AS ts_code,
       b.market || b.code        AS gw_code,
       b.name,
       b.board,
       b.status,
       b.is_st,
       b.pinyin_abbr,
       b.list_date,
       b.delist_date,
       i.l1 AS industry_l1,
       i.l2 AS industry_l2,
       i.l3 AS industry_l3,
       COALESCE(c.concepts, ARRAY[]::text[]) AS concepts
FROM stock_basic b
LEFT JOIN stock_industry i ON i.code = b.code AND i.std = 'em'
LEFT JOIN LATERAL (
  SELECT array_agg(sc.concept ORDER BY sc.concept) AS concepts
  FROM stock_concept sc
  WHERE sc.code = b.code
) c ON TRUE;
