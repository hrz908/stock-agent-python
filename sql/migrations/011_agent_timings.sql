-- agent_runs 增加阶段计时与首个事件耗时
--
-- 背景：原 ttft_ms 由「首个 custom 事件」触发，tool_start / sources 会提前
-- 把首响记低；同时缺少 rewrite/classify/检索/工具/生成各段耗时，无法定位
-- 68s 这类长尾。新增：
--   first_event_ms : 首个用户可见事件（含 tool_start/sources）的耗时
--   timings        : 阶段计时 JSONB（rewrite_ms / classify_ms / embed_ms /
--                    sql_ms / rerank_ms / judge_ms / tool_call_ms /
--                    tool_execute_ms / synth_ttft_ms / synth_ms 等）
--
-- 已在运行的库需手工执行本文件；新库由 docker-entrypoint-initdb.d 顺序执行。

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS first_event_ms INT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS timings JSONB NOT NULL DEFAULT '{}'::jsonb;
