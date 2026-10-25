package db

import (
	"context"
	"log"
	"time"

	"github.com/redis/go-redis/v9"
)

// redisPingTimeout 启动探活超时；独立于 main 的 signal context，避免 Redis 挂起拖住启动。
const redisPingTimeout = 3 * time.Second

// NewRedis 建立 Redis 连接；poolSize > 0 时显式设置池大小（<= 0 用 go-redis 默认 10×CPU）。
// Redis 在本项目只承担缓存与限流，连不上不阻断启动，业务侧自行降级。
func NewRedis(_ context.Context, addr, password string, db, poolSize int) *redis.Client {
	client := redis.NewClient(&redis.Options{
		Addr:     addr,
		Password: password,
		DB:       db,
		PoolSize: poolSize,
	})
	ctx, cancel := context.WithTimeout(context.Background(), redisPingTimeout)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		log.Printf("redis ping failed (degrade mode): %v", err)
	}
	return client
}
