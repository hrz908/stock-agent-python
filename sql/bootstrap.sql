-- 网关自管的三张表（users / user_watchlist / alert_rule）——**唯一 DDL 来源**。
-- 服务启动时执行一次（server/main.py），幂等：已存在的库不会被改动，新库能直接建起来。
-- 其余表（chunks / stock_basic / agent_runs …）由 sql/migrations/*.sql 负责，
-- 两者互不重叠：这里不写业务表，迁移文件也不写这三张表。

CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL PRIMARY KEY,
    openid      VARCHAR(64)  NOT NULL,
    nickname    VARCHAR(64),
    avatar      VARCHAR(512),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_users_openid ON users (openid);

CREATE TABLE IF NOT EXISTS user_watchlist (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT       NOT NULL,
    stock_code  VARCHAR(20)  NOT NULL,
    stock_name  VARCHAR(100),
    -- 金额一律定点数，禁止 float：成本价按 4 位小数存
    cost_price  NUMERIC(18, 4) NOT NULL DEFAULT 0,
    quantity    INTEGER      NOT NULL DEFAULT 0,
    -- 软删除：移出自选只置 false，不物理删行
    enabled     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_user_stock ON user_watchlist (user_id, stock_code);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON user_watchlist (user_id);

CREATE TABLE IF NOT EXISTS alert_rule (
    id                 BIGSERIAL PRIMARY KEY,
    user_id            BIGINT       NOT NULL,
    stock_code         VARCHAR(20)  NOT NULL,
    stock_name         VARCHAR(100),
    condition_type     VARCHAR(20)  NOT NULL,
    threshold          NUMERIC(18, 4) NOT NULL,
    volume_base        INTEGER      NOT NULL DEFAULT 0,
    check_frequency    VARCHAR(16)  NOT NULL DEFAULT 'realtime',
    enabled            BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_triggered_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_alert_user_stock_cond
    ON alert_rule (user_id, stock_code, condition_type);
CREATE INDEX IF NOT EXISTS idx_alert_user ON alert_rule (user_id);
