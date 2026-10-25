package main

import (
	"context"
	"log"
	"net/http"
	"os/signal"
	"slices"
	"syscall"

	"stock-agent-server/internal/agent"
	"stock-agent-server/internal/auth"
	"stock-agent-server/internal/config"
	"stock-agent-server/internal/db"
	"stock-agent-server/internal/deps"
	"stock-agent-server/internal/model"
	"stock-agent-server/internal/module/alert"
	"stock-agent-server/internal/module/watchlist"
	"stock-agent-server/internal/quote"
	"stock-agent-server/internal/router"
	"stock-agent-server/internal/upload"
	"stock-agent-server/internal/wechat"
	"stock-agent-server/internal/wecom"
)

func main() {
	cfg := config.Load()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// prod 安全底线：JWT 密钥仍是默认值等于人人可伪造 token；
	// 微信凭据缺失会让所有用户共用一个演示账号、数据串号。
	// dev 两者都允许（本地联调方便），prod 一律拒绝启动
	if cfg.Env == "prod" {
		if cfg.JWTSecret == "" || cfg.JWTSecret == "please-change-me" {
			log.Fatal("JWT_SECRET 未设置或仍是默认值，禁止以 prod 启动")
		}
		if cfg.WechatAppID == "" || cfg.WechatSecret == "" {
			log.Fatal("WECHAT_APPID / WECHAT_SECRET 未配置，禁止以 prod 启动（否则所有用户共用一个演示账号）")
		}
	}

	pg, err := db.NewPostgres(cfg.DSN, db.PoolParams{
		MaxOpenConns:    cfg.DB.MaxOpenConns,
		MaxIdleConns:    cfg.DB.MaxIdleConns,
		ConnMaxLifetime: cfg.DB.ConnMaxLifetime,
		ConnMaxIdleTime: cfg.DB.ConnMaxIdleTime,
	})
	if err != nil {
		log.Fatalf("init postgres: %v", err)
	}
	// 各模块的模型在这里汇总：新增模块时把它导出的 Models 加进这个切片
	models := slices.Concat(watchlist.Models, alert.Models, model.Models)
	if err := db.AutoMigrate(pg, models...); err != nil {
		log.Fatalf("auto migrate: %v", err)
	}

	rdb := db.NewRedis(ctx, cfg.RedisAddr, cfg.RedisPassword, cfg.RedisDB, cfg.Redis.PoolSize)
	deps.Init(pg, rdb)

	auth.Init(cfg.JWTSecret, cfg.JWTTTL)
	quote.Init(cfg.QuoteAPI,
		cfg.HTTP.QuoteTimeout, cfg.HTTP.SearchTimeout, cfg.HTTP.QuoteCacheTTL,
		cfg.HTTP.QuoteFetchLimit, cfg.HTTP.QuoteBatchSize, cfg.HTTP.QuoteMgetChunk)
	// 小程序凭据：两个都有值时登录走微信 code2session，否则退化为演示账号
	wechat.Init(cfg.WechatAppID, cfg.WechatSecret, cfg.HTTP.WechatTimeout)
	// 企业微信：应用消息（@all，需可信 IP）与群机器人 Webhook 两条提醒通道
	wecom.Init(cfg.WeComCorpID, cfg.WeComSecret, cfg.WeComAgentID, cfg.WeComWebhook, cfg.HTTP.WecomTimeout)
	if err := upload.Init(cfg.UploadDir); err != nil {
		log.Fatalf("init upload dir: %v", err)
	}

	// Agent 网关：注入 Redis（三道限流闸门）与服务地址、运行参数。
	// 必须在 router.New 之前完成，否则路由 handler 取到的 limiter / proxy 为 nil
	agent.Init(rdb, agent.Options{
		BaseURL:          cfg.AgentBaseURL,
		QPS:              cfg.Agent.QPS,
		Concurrent:       cfg.Agent.Concurrent,
		DailyQuota:       cfg.Agent.DailyQuota,
		ProxyConcurrency: cfg.Agent.ProxyConcurrency,
		RunTimeout:       cfg.Agent.RunTimeout,
		TTFTTimeout:      cfg.Agent.TTFTTimeout,
		MaxLineBytes:     cfg.Agent.MaxLineMB << 20,
	})

	srv := &http.Server{
		Addr:    cfg.Addr,
		Handler: router.New(cfg),
		// 不设超时的话，慢速发送请求头的连接可长期占住资源（Slowloris）
		ReadHeaderTimeout: cfg.Server.ReadHeaderTimeout,
		ReadTimeout:       cfg.Server.ReadTimeout,
		WriteTimeout:      cfg.Server.WriteTimeout,
		IdleTimeout:       cfg.Server.IdleTimeout,
	}

	// ListenAndServe 的错误经通道回传：在 goroutine 里 log.Fatalf 会跳过 defer、
	// 绕过优雅关闭路径（连接池 / Redis 客户端都不会收尾）
	serverErr := make(chan error, 1)
	go func() {
		log.Printf("server listening on %s (env=%s)", cfg.Addr, cfg.Env)
		serverErr <- srv.ListenAndServe()
	}()

	select {
	case <-ctx.Done():
	case err := <-serverErr:
		if err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %v", err)
		}
		return
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.Server.ShutdownTimeout)
	defer cancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
	// 连接池与 Redis 客户端收尾，避免退出时悬挂连接
	if sqlDB, err := pg.DB(); err == nil {
		_ = sqlDB.Close()
	}
	if rdb != nil {
		_ = rdb.Close()
	}
	log.Printf("server stopped")
}
