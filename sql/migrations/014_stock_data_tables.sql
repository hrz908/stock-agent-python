-- 数据类（数值）业务表：每日估值快照 + 采集水位
--
-- 定位：数值问答走这里，不走向量库。向量库只回答定性问题（业务/风险/观点/政策），
-- 从 450 字的文本块里抽数字既不准也慢，且表格型内容分块后召回很差。
--
-- 采集范围与向量语料一致：ashare_universe（当前成交额 Top300 × 最近一年）。
-- 主数据表 stock_basic / stock_industry / stock_concept 是全市场身份表，不受此范围影响。

-- 每日估值快照。stock_basic 里明确不存估值（每天变动、落库即过时），
-- 但「历史 PE 分位 / 最近一个月估值走势」这类问题恰恰需要历史，所以单独一张日表。
-- 单位全部进列名：金额为元，*_pct 为百分数。agent 侧卡片契约用万元，换算在读取层做。
CREATE TABLE IF NOT EXISTS stock_valuation_daily (
  code          VARCHAR(6) NOT NULL REFERENCES stock_basic(code) ON DELETE CASCADE,
  trade_date    DATE NOT NULL,
  close_yuan    NUMERIC(12,4),               -- 当日收盘价
  pe_static     NUMERIC(14,4),               -- PE(静)
  pe_ttm        NUMERIC(14,4),               -- PE(TTM)
  pb            NUMERIC(14,4),               -- 市净率
  ps_ttm        NUMERIC(14,4),               -- 市销率
  dv_ttm_pct    NUMERIC(10,4),               -- 股息率；东财估值接口无此字段时为 NULL，不编造
  total_mv_yuan NUMERIC(22,2),               -- 总市值
  circ_mv_yuan  NUMERIC(22,2),               -- 流通市值
  source        VARCHAR(16) NOT NULL DEFAULT 'em',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (code, trade_date)
);

-- 全市场按日横切（「今天 PE 最低的 50 只」）比按个股查更常见
CREATE INDEX IF NOT EXISTS stock_val_trade_date ON stock_valuation_daily (trade_date DESC);

-- 每个数据集一行的同步水位。失败不推进水位，下次重跑同一段，
-- 重复行由业务主键 upsert 挡掉（与 ashare_sync_state 的增量思路一致）。
CREATE TABLE IF NOT EXISTS stock_data_sync_state (
  dataset      TEXT PRIMARY KEY,             -- finance / valuation
  watermark    TEXT NOT NULL DEFAULT '',     -- 最近一个完整跑的区间：finance=报告期 valuation=交易日
  last_run_at  TIMESTAMPTZ,
  last_ok_at   TIMESTAMPTZ,
  rows_written BIGINT NOT NULL DEFAULT 0,
  error        TEXT
);
