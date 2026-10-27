-- 提醒表升级：对齐小程序「新增提醒」弹窗的字段
-- 新增：股票名称、阈值（原 condition_value）、成交量基准周期、检查频率、更新时间
-- 老的条件类型 price_gte / price_lte 换成 price_up / price_down
--
-- 说明：新库不需要跑这个脚本 —— main.go 里的 AutoMigrate 会按 model 直接建出
-- 新结构。这个脚本只用于「已经有老 alert_rule 表的库」就地升级
ALTER TABLE alert_rule ADD COLUMN IF NOT EXISTS stock_name      VARCHAR(100);
ALTER TABLE alert_rule ADD COLUMN IF NOT EXISTS threshold       NUMERIC(18, 4);
ALTER TABLE alert_rule ADD COLUMN IF NOT EXISTS volume_base     INT DEFAULT 0;
ALTER TABLE alert_rule ADD COLUMN IF NOT EXISTS check_frequency VARCHAR(16) DEFAULT 'realtime';
ALTER TABLE alert_rule ADD COLUMN IF NOT EXISTS updated_at     TIMESTAMPTZ DEFAULT NOW();

-- 老数据搬运：目标价 -> 阈值，旧条件类型 -> 新条件类型
UPDATE alert_rule SET threshold = condition_value WHERE threshold IS NULL;
UPDATE alert_rule SET condition_type = 'price_up' WHERE condition_type = 'price_gte';
UPDATE alert_rule SET condition_type = 'price_down' WHERE condition_type = 'price_lte';

-- 极少数脏数据（历史行的 condition_value 为空）兜底填 0，否则下面的非空约束会失败
UPDATE alert_rule SET threshold = 0 WHERE threshold IS NULL;

-- 阈值补完后补上非空约束，再丢掉废弃的目标价列
ALTER TABLE alert_rule ALTER COLUMN threshold SET NOT NULL;
ALTER TABLE alert_rule DROP COLUMN IF EXISTS condition_value;
