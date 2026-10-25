package agent

import (
	"time"

	"github.com/redis/go-redis/v9"
)

// Options 网关运行参数，由 config.agent.* 注入。
type Options struct {
	BaseURL          string        // Python Agent 服务基地址
	QPS              int           // 单用户每秒令牌数，兼作桶容量
	Concurrent       int           // 单用户并发运行数
	DailyQuota       int           // 单用户每日请求配额
	ProxyConcurrency int           // 打向 Python Agent 的并发上限
	RunTimeout       time.Duration // 单次运行整体超时
	TTFTTimeout      time.Duration // 首字节等待上限
	MaxLineBytes     int           // ndjson 单行上限
}

// 模块级单例，与 auth/deps 保持「main 注入一次、业务包直接取用」的风格。
// 网关本身无状态（限流计数在 Redis），单例足够。
var (
	limiter *Limiter
	proxy   *AgentProxy
)

// Init 初始化网关，须在路由组装与请求到来前调用一次。
// redisClient 承载三道限流闸门，opts.BaseURL 指向 Python Agent 服务；
// 各字段为零值时回落到内置默认值。
func Init(redisClient *redis.Client, opts Options) {
	if opts.QPS <= 0 {
		opts.QPS = DefaultLimits.QPS
	}
	if opts.Concurrent <= 0 {
		opts.Concurrent = DefaultLimits.Concurrent
	}
	if opts.DailyQuota <= 0 {
		opts.DailyQuota = DefaultLimits.DailyQuota
	}
	if opts.ProxyConcurrency <= 0 {
		opts.ProxyConcurrency = 64
	}
	if opts.RunTimeout <= 0 {
		opts.RunTimeout = 120 * time.Second
	}
	if opts.TTFTTimeout <= 0 {
		opts.TTFTTimeout = 8 * time.Second
	}
	if opts.MaxLineBytes <= 0 {
		opts.MaxLineBytes = 4 << 20
	}

	limiter = NewLimiter(redisClient, Limits{QPS: opts.QPS, Concurrent: opts.Concurrent, DailyQuota: opts.DailyQuota})
	proxy = NewAgentProxy(opts)
}
