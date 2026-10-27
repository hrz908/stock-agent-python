-- 提醒规则复合唯一索引：(user_id, stock_code, condition_type)
--
-- 应用层的「先查后建」挡不住并发：两个请求同时通过存在性检查就会插入两条
-- 相同规则，之后每次刷新都会推两条重复提醒。唯一约束交给 DB 兜底，
-- 冲突时 handler 返回 409（GORM 开了 TranslateError，能识别 ErrDuplicatedKey）
--
-- 历史脏数据先去重（保留最小 id），否则建索引会失败
DELETE FROM alert_rule a
USING alert_rule b
WHERE a.id > b.id
  AND a.user_id = b.user_id
  AND a.stock_code = b.stock_code
  AND a.condition_type = b.condition_type;

CREATE UNIQUE INDEX IF NOT EXISTS uk_alert_user_stock_cond
  ON alert_rule(user_id, stock_code, condition_type);
