-- 网关自管的三张表（原 Go 版由 gorm AutoMigrate 创建，这里改为等价的幂等 DDL）。
-- 启动时执行一次。AutoMigrate 只新增列/索引、不改名不删列，这里的 IF NOT EXISTS
-- 语义与之等价：已存在的库不会被改动，新库能直接建起来。
-- 其余表（chunks / stock_basic / agent_runs …）由 migrations/*.sql 与 agent 侧负责。

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
