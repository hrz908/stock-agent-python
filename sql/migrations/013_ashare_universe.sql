-- 热门股票池（成交额 Top N）+ 五年回填进度
--
-- 为什么单独建表而不是复用 stock_basic：
-- stock_basic 是「全市场身份表」，语义是全集，每天按成交额删改它会把退市判定、
-- 行业外键一起带歪；热门池只是「当前采集范围」，会每天整体替换，两者生命周期不同。
--
-- 为什么不建外键到 stock_basic：主数据是 akshare 同步的独立链路，慢一步没同步时
-- 热门池不该插不进去；真实成分由代码本身保证，外键只增加互相阻塞。

CREATE TABLE IF NOT EXISTS ashare_universe (
  code       VARCHAR(6) PRIMARY KEY CHECK (code ~ '^[0-9]{6}$'),
  rank       INT NOT NULL,                 -- 1 = 成交额最大
  name       TEXT,
  amount     NUMERIC(20, 2),               -- 当日成交额（元）
  turnover   NUMERIC(10, 4),               -- 换手率（%）
  change_pct NUMERIC(10, 4),               -- 涨跌幅（%）
  trade_date DATE NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ashare_universe_rank ON ashare_universe (rank);

-- 每日快照：热门池每天整体替换，不留下快照就无法回答「上周这个池子里有谁」，
-- 也无法回填「按当时的热门股」这一口径。500 行/天，一年不到 20 万行，代价可忽略。
CREATE TABLE IF NOT EXISTS ashare_universe_history (
  trade_date DATE NOT NULL,
  code       VARCHAR(6) NOT NULL,
  rank       INT NOT NULL,
  amount     NUMERIC(20, 2),
  PRIMARY KEY (trade_date, code)
);

CREATE INDEX IF NOT EXISTS ashare_universe_history_code ON ashare_universe_history (code, trade_date DESC);

-- 五年回填的断点表：按「源 + 窗口」记录，重跑时跳过已完成窗口。
-- 回填窗口很碎（每月一窗 × 多个源），靠水位单值无法表达，必须单独记。
CREATE TABLE IF NOT EXISTS ashare_backfill_progress (
  source       TEXT NOT NULL,
  -- 采集范围：universe / codes:<hash>。同一窗口用不同股票范围跑过，产出不同，
  -- 断点必须按范围隔离，否则试跑过的窗口会让正式回填误跳过。
  scope        TEXT NOT NULL DEFAULT '',
  window_start DATE NOT NULL,
  window_end   DATE NOT NULL,
  status       TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('done', 'failed')),
  docs         INT NOT NULL DEFAULT 0,
  chunks       INT NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, scope, window_start, window_end)
);

CREATE INDEX IF NOT EXISTS ashare_backfill_progress_source ON ashare_backfill_progress (source, window_start DESC);

CREATE OR REPLACE VIEW ashare_universe_summary AS
SELECT u.trade_date,
       count(*)                    AS codes,
       sum(u.amount) / 100000000.0 AS amount_yi,
       min(u.rank)                 AS min_rank,
       max(u.rank)                 AS max_rank
FROM ashare_universe u
GROUP BY u.trade_date;
