package router

import (
	"path/filepath"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"stock-agent-server/internal/agent"
	"stock-agent-server/internal/config"
	"stock-agent-server/internal/middleware"
	"stock-agent-server/internal/module/alert"
	authapi "stock-agent-server/internal/module/auth"
	"stock-agent-server/internal/module/stock"
	"stock-agent-server/internal/module/user"
	"stock-agent-server/internal/module/watchlist"
	"stock-agent-server/internal/resp"
	"stock-agent-server/internal/upload"
)

// New 组装路由：全局中间件按序执行（Recovery → RequestID → CORS → RateLimit → AccessLog），
// /api/v1 业务路由统一挂 auth；各模块自带 Register，新增接口在此 import 并注册一次。
func New(cfg config.Config) *gin.Engine {
	r := gin.New()

	// 只信任配置里的反代：gin 默认信任所有代理的 X-Forwarded-For，否则按 IP 限流可被伪造头绕过；
	// 生产 nginx 在宿主机、server 在容器里，需把 docker 网桥网段一并配进来
	if err := r.SetTrustedProxies(trustedProxies(cfg.TrustedProxies)); err != nil {
		panic(err)
	}

	// Recovery 必须最先注册，才能兜住后续中间件与 Handler 的 panic
	r.Use(middleware.Recovery())
	r.Use(middleware.RequestID())
	r.Use(middleware.CORS(cfg.AllowOrigins))
	// 限流放在鉴权之前：未登录的刷接口也要被挡住（Redis 客户端由 deps 提供）
	r.Use(middleware.IPRateLimit(cfg.RateLimit.IPPerMin, time.Minute))
	r.Use(middleware.AccessLog())

	// 健康检查给容器 / 编排探针用，不鉴权也不该被限流误伤
	r.GET("/healthz", func(c *gin.Context) {
		resp.OK(c, gin.H{"status": "ok"})
	})

	// 只暴露 avatars 子目录：挂整个 upload.Dir 会让目录路径返回文件列表（信息泄露），
	// 且读写共用同一个 upload.Dir，不会出现「写到 A、从 B 读」的错位
	r.Static("/uploads/avatars", filepath.Join(upload.Dir, "avatars"))

	// 业务接口的请求体都是小 JSON（头像上传 2MB、对话 64KB 各自另有上限），
	// 4MB 兜住 ShouldBindJSON 把超大 body 读进内存的情况
	api := r.Group("/api/v1", middleware.BodyLimit(4<<20))

	// 公开路由：登录（内部另挂了更严的限流）
	authapi.Register(api, cfg.RateLimit.LoginPerMin)

	// 受保护路由：统一挂 auth（Header Bearer），user_id 由中间件注入
	authorized := api.Group("")
	authorized.Use(middleware.Auth())
	{
		watchlist.Register(authorized, cfg.WatchlistMaxItems)
		alert.Register(authorized)
		user.Register(authorized)

		// 行情与搜索会放大成多个第三方上游请求（腾讯行情 / smartbox），故按用户单独限流
		market := authorized.Group("")
		market.Use(middleware.UserRateLimit(cfg.RateLimit.MarketPerMin, time.Minute))
		stock.Register(market)

		// Agent 对话 SSE：小程序与 H5 共用同一条链路，走 Header 鉴权
		authorized.POST("/agent/stream", agent.ServeSSE)

		// 会话历史：列表 / 注册 / 恢复消息 / 删除；注册表与 checkpoint 由 Agent 侧持有，网关只转发
		authorized.GET("/agent/sessions", agent.ServeListSessions)
		authorized.POST("/agent/sessions", agent.ServeCreateSession)
		authorized.GET("/agent/sessions/:sid/messages", agent.ServeSessionMessages)
		authorized.DELETE("/agent/sessions/:sid", agent.ServeDeleteSession)
	}

	return r
}

// trustedProxies 解析逗号分隔的反代地址；空配置回落到仅本机。
func trustedProxies(raw string) []string {
	out := make([]string, 0, 4)
	for _, p := range strings.Split(raw, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	if len(out) == 0 {
		return []string{"127.0.0.1"}
	}
	return out
}
