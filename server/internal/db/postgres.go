package db

import (
	"context"
	"time"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

const pingTimeout = 3 * time.Second

// PoolParams 连接池参数，由 config.db.* 注入。
type PoolParams struct {
	MaxOpenConns    int
	MaxIdleConns    int
	ConnMaxLifetime time.Duration
	ConnMaxIdleTime time.Duration
}

// NewPostgres 建立 GORM 连接，配好连接池并立即探活，
// 让连接串写错这类问题在启动时暴露，而不是等第一个业务请求。
func NewPostgres(dsn string, pool PoolParams) (*gorm.DB, error) {
	gdb, err := gorm.Open(postgres.Open(dsn), &gorm.Config{
		Logger:         logger.Default.LogMode(logger.Warn),
		TranslateError: true,
	})
	if err != nil {
		return nil, err
	}

	sqlDB, err := gdb.DB()
	if err != nil {
		return nil, err
	}
	// 不设上限时突发流量会把 Postgres 连接打满；ConnMaxIdleTime 让空闲连接
	// 早于服务端/中间设备的连接老化阈值被回收，避免拿到死连接
	sqlDB.SetMaxOpenConns(pool.MaxOpenConns)
	sqlDB.SetMaxIdleConns(pool.MaxIdleConns)
	sqlDB.SetConnMaxLifetime(pool.ConnMaxLifetime)
	sqlDB.SetConnMaxIdleTime(pool.ConnMaxIdleTime)

	ctx, cancel := context.WithTimeout(context.Background(), pingTimeout)
	defer cancel()
	if err := sqlDB.PingContext(ctx); err != nil {
		return nil, err
	}
	return gdb, nil
}

// AutoMigrate 建表并补索引。模型由各业务模块导出（module/xxx.Models），
// main 汇总后传入，新增模块无需改本文件。
// 只会新增列/索引，不改名也不删列；老库结构升级走 migrations/ 脚本（配合 compose 的 initdb）。
func AutoMigrate(db *gorm.DB, models ...any) error {
	if len(models) == 0 {
		return nil
	}
	return db.AutoMigrate(models...)
}
