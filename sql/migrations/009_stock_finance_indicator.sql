-- A股报告期财务宽表（P1，可按需启用）
--
-- 只收「财报卡 + 基本面问答」用到的字段：完整三大报表不落库（列多、口径杂、利用率低），
-- 需要更多科目时再走 akshare 或加列。
--
-- 单位全部写进列名：eps/bps 为元，*_pct 为百分数（12.5 表示 12.5%），金额为元。
-- 项目里 total_mv 已经出现过「Go 侧元 / akshare 侧万元」两套口径，新表不再欠这笔债。
--
-- 口径：同一报告期的比率是**累计值**（Q3 = 前三季度），不是单季；跨期比较前先确认这一点。
-- announce_date 用于 as-of 语义：按公告日过滤，避免用还没披露的数据回答历史问题。

CREATE TABLE IF NOT EXISTS stock_finance_indicator (
  code           VARCHAR(6) NOT NULL REFERENCES stock_basic(code) ON DELETE CASCADE,
  report_date    DATE NOT NULL,
  report_type    VARCHAR(4) NOT NULL CHECK (report_type IN ('Q1', 'H1', 'Q3', 'FY')),
  announce_date  DATE,
  eps_yuan             NUMERIC(12,4),   -- 摊薄每股收益
  bps_yuan             NUMERIC(12,4),   -- 每股净资产（调整后）
  roe_pct              NUMERIC(10,4),
  gross_margin_pct     NUMERIC(10,4),
  net_margin_pct       NUMERIC(10,4),
  debt_asset_pct       NUMERIC(10,4),
  revenue_yoy_pct      NUMERIC(12,4),
  netprofit_yoy_pct    NUMERIC(12,4),
  -- 财报卡需要的三个流量数：营收、归母净利润、经营现金流净额
  revenue_yuan         NUMERIC(20,2),
  netprofit_yuan       NUMERIC(20,2),
  ocf_yuan             NUMERIC(20,2),
  source         VARCHAR(16) NOT NULL DEFAULT 'sina',
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (code, report_date)
);

-- 全市场按报告期横向筛（「最新一期 ROE 前 50」）比按个股查更常见
CREATE INDEX IF NOT EXISTS stock_fin_report_date ON stock_finance_indicator (report_date DESC);
CREATE INDEX IF NOT EXISTS stock_fin_roe ON stock_finance_indicator (report_date DESC, roe_pct DESC);
