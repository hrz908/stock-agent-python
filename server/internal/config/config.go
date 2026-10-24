// Package config 统一配置层，优先级：环境变量 > config.yaml（可选）> 内置默认值。
//
// 键名为扁平 snake_case，配合 SetEnvKeyReplacer(".", "_") 与无前缀 AutomaticEnv，
// 旧环境变量（ADDR / DSN / JWT_SECRET / REDIS_ADDR…）原样生效。
// 原先散落在各包的硬编码常量（限流、超时、连接池、批量参数、WS 闸门等）全部上收为本包字段，
// main 启动时统一注入，运行期不再改档。
package config

import (
	"errors"
	"log"
	"os"
	"strings"
	"time"

	"github.com/spf13/viper"
)

// ServerConfig HTTP 服务器参数。
type ServerConfig struct {
	ReadHeaderTimeout time.Duration `mapstructure:"read_header_timeout"`
	ReadTimeout       time.Duration `mapstructure:"read_timeout"`
	WriteTimeout      time.Duration `mapstructure:"write_timeout"`
	IdleTimeout       time.Duration `mapstructure:"idle_timeout"`
	ShutdownTimeout   time.Duration `mapstructure:"shutdown_timeout"`
}

// DBConfig PostgreSQL 连接池参数（GORM 底层 sql.DB）。
type DBConfig struct {
	MaxOpenConns    int           `mapstructure:"max_open_conns"`
	MaxIdleConns    int           `mapstructure:"max_idle_conns"`
	ConnMaxLifetime time.Duration `mapstructure:"conn_max_lifetime"`
	ConnMaxIdleTime time.Duration `mapstructure:"conn_max_idle_time"`
}

// RedisConfig Redis 客户端参数；PoolSize <= 0 时用 go-redis 默认值（10×CPU）。
type RedisConfig struct {
	PoolSize int `mapstructure:"pool_size"`
}

// RateLimitConfig 限流档位（每分钟次数，Redis 滑动窗口）。
type RateLimitConfig struct {
	IPPerMin     int64 `mapstructure:"ip_per_min"`     // 全局按 IP
	LoginPerMin  int64 `mapstructure:"login_per_min"`  // 登录接口
	MarketPerMin int64 `mapstructure:"market_per_min"` // 行情/搜索（登录用户）
	ASRPerMin    int64 `mapstructure:"asr_per_min"`    // 语音转文字（消耗上游付费额度）
}

// AgentConfig Agent 网关参数：三道闸门 + 代理并发 + WS 闸门 + 运行时限。
type AgentConfig struct {
	QPS              int           `mapstructure:"qps"`               // 单用户每秒令牌数
	Concurrent       int           `mapstructure:"concurrent"`        // 单用户并发运行数
	DailyQuota       int           `mapstructure:"daily_quota"`       // 单用户每日配额
	ProxyConcurrency int           `mapstructure:"proxy_concurrency"` // 打向 Python Agent 的并发上限
	WSMaxConns       int           `mapstructure:"ws_max_conns"`      // 进程内 WS 连接上限
	RunTimeout       time.Duration `mapstructure:"run_timeout"`       // 单次运行整体超时
	TTFTTimeout      time.Duration `mapstructure:"ttft_timeout"`      // 首字节等待上限
	MaxLineMB        int           `mapstructure:"max_line_mb"`       // ndjson 单行上限（MB）
}

// HTTPConfig 上游 HTTP 客户端超时与行情批量参数。
type HTTPConfig struct {
	QuoteTimeout    time.Duration `mapstructure:"quote_timeout"`
	SearchTimeout   time.Duration `mapstructure:"search_timeout"`
	WechatTimeout   time.Duration `mapstructure:"wechat_timeout"`
	WecomTimeout    time.Duration `mapstructure:"wecom_timeout"`
	ASRTimeout      time.Duration `mapstructure:"asr_timeout"`
	QuoteCacheTTL   time.Duration `mapstructure:"quote_cache_ttl"`
	QuoteFetchLimit int           `mapstructure:"quote_fetch_limit"`
	QuoteBatchSize  int           `mapstructure:"quote_batch_size"`
	QuoteMgetChunk  int           `mapstructure:"quote_mget_chunk"`
}

// HubConfig 行情提醒 WS hub 连接上限。
type HubConfig struct {
	MaxPerUser int `mapstructure:"max_per_user"`
	MaxTotal   int `mapstructure:"max_total"`
}

// TencentASRConfig 腾讯云实时语音识别凭据；三项缺一不可，
// 未配置时 /ws/asr 返回 503，小程序自动回退整段上传识别。
type TencentASRConfig struct {
	AppID     string `mapstructure:"app_id"`
	SecretID  string `mapstructure:"secret_id"`
	SecretKey string `mapstructure:"secret_key"`
	Engine    string `mapstructure:"engine"`
	MaxConns  int    `mapstructure:"max_conns"`
}

// Config 运行期配置。顶层键与旧环境变量同名（addr/dsn/jwt_secret…），
// 分组键经 replacer 映射为下划线环境变量（server.read_timeout -> SERVER_READ_TIMEOUT）。
type Config struct {
	Addr          string        `mapstructure:"addr"` // HTTP 监听地址
	DSN           string        `mapstructure:"dsn"`  // PostgreSQL 连接串
	JWTSecret     string        `mapstructure:"jwt_secret"`
	JWTTTL        time.Duration `mapstructure:"jwt_ttl"` // 登录 token 有效期
	RedisAddr     string        `mapstructure:"redis_addr"`
	RedisPassword string        `mapstructure:"redis_password"`
	RedisDB       int           `mapstructure:"redis_db"`

	QuoteAPI     string `mapstructure:"quote_api"`     // 行情接口基地址；为空时用内置腾讯源
	WechatAppID  string `mapstructure:"wechat_appid"`  // 与 Secret 同时为空时，dev 下登录退化为演示账号
	WechatSecret string `mapstructure:"wechat_secret"` //
	WeComCorpID  string `mapstructure:"wecom_corp_id"` // 企业微信企业 ID（corpid）
	WeComSecret  string `mapstructure:"wecom_secret"`
	WeComAgentID int    `mapstructure:"wecom_agent_id"`
	// WeComWebhook 群机器人地址：不需要 access_token 与可信 IP，与应用消息并行推送
	WeComWebhook string `mapstructure:"wecom_webhook"`

	UploadDir    string `mapstructure:"upload_dir"`
	Env          string `mapstructure:"env"`           // dev（默认）/ prod；prod 强制校验 JWT_SECRET 与微信凭据
	AllowOrigins string `mapstructure:"allow_origins"` // CORS Origin，逗号分隔，* 表示全部

	AgentBaseURL string `mapstructure:"agent_base_url"` // Python Agent 基地址，网关代理到其 /run/stream
	ASRBaseURL   string `mapstructure:"asr_base_url"`   // 语音转文字上游（OpenAI 兼容 /audio/transcriptions）
	ASRAPIKey    string `mapstructure:"asr_api_key"`    // 上游密钥；为空时 /asr/transcribe 返回「未配置」
	ASRModel     string `mapstructure:"asr_model"`      // 上游 ASR 模型名

	TencentASR TencentASRConfig `mapstructure:"tencent_asr"` // 腾讯云实时语音识别（流式）

	Server            ServerConfig    `mapstructure:"server"`
	DB                DBConfig        `mapstructure:"db"`
	Redis             RedisConfig     `mapstructure:"redis"`
	RateLimit         RateLimitConfig `mapstructure:"ratelimit"`
	Agent             AgentConfig     `mapstructure:"agent"`
	HTTP              HTTPConfig      `mapstructure:"http"`
	Hub               HubConfig       `mapstructure:"hub"`
	WatchlistMaxItems int             `mapstructure:"watchlist_max_items"`
}

// Load 读取配置：默认值打底 → 可选 config.yaml → 环境变量覆盖。
// 非法 int/duration 直接启动失败，不允许静默回退把问题藏到运行期。
func Load() Config {
	// 本地开发兜底：把 server/.env 注入进程环境（真实环境变量优先，文件缺失不报错）
	loadDotEnv(".env")
	setDefaults()

	// 无前缀 + 点转下划线，兼容既有环境变量名（ADDR、REDIS_ADDR、JWT_SECRET…）
	viper.SetEnvKeyReplacer(strings.NewReplacer(".", "_"))
	viper.AutomaticEnv()

	viper.SetConfigName("config")
	viper.SetConfigType("yaml")
	viper.AddConfigPath(".")
	if err := viper.ReadInConfig(); err != nil {
		// 配置文件缺失属正常形态，其余解析错误必须暴露。
		// 注意 viper v1.x 返回的是 ConfigFileNotFoundError 值类型，
		// 用指针做 errors.As 永远匹配不上，会把「没写配置文件」误判成致命错误。
		var notFound viper.ConfigFileNotFoundError
		if !errors.As(err, &notFound) {
			log.Fatalf("read config: %v", err)
		}
	}

	var cfg Config
	if err := viper.Unmarshal(&cfg); err != nil {
		log.Fatalf("unmarshal config: %v", err)
	}

	// ASR 密钥回退 OPENAI_API_KEY：两者多同一上游同一把钥匙，配一次即可
	if cfg.ASRAPIKey == "" {
		cfg.ASRAPIKey = os.Getenv("OPENAI_API_KEY")
	}
	return cfg
}

// setDefaults 注册全部默认值（取值即迁移前的硬编码常量）。
func setDefaults() {
	d := viper.SetDefault
	d("addr", ":8080")
	d("dsn", "postgres://postgres:postgres@localhost:5432/stock_agent?sslmode=disable")
	d("redis_addr", "127.0.0.1:6379")
	d("redis_password", "")
	d("redis_db", 0)
	d("jwt_secret", "please-change-me")
	d("jwt_ttl", 7*24*time.Hour)
	d("quote_api", "")
	d("wechat_appid", "")
	d("wechat_secret", "")
	// 企业微信自建应用凭据：不设默认值，必须来自 .env。
	// 曾把真实 corp_id/secret/agent_id 写在这里，而仓库是公开的——按已泄露处理。
	// 三项缺任意一项，wecom.Enabled() 为 false，只是不推送，不影响站内提醒。
	d("wecom_corp_id", "")
	d("wecom_secret", "")
	d("wecom_agent_id", 0)
	d("wecom_webhook", "")
	d("upload_dir", "./uploads")
	d("env", "dev")
	d("allow_origins", "*")
	d("agent_base_url", "http://127.0.0.1:8000")
	// 语音转文字默认指向硅基流动；密钥无默认值，不配即不可用，避免静默拿到空结果
	d("asr_base_url", "https://api.siliconflow.cn/v1")
	d("asr_api_key", "")
	d("asr_model", "FunAudioLLM/SenseVoiceSmall")

	// 腾讯云实时语音识别：走 wss://asr.cloud.tencent.com，密钥只在服务端，不下发到小程序包
	d("tencent_asr.app_id", "")
	d("tencent_asr.secret_id", "")
	d("tencent_asr.secret_key", "")
	d("tencent_asr.engine", "16k_zh")
	d("tencent_asr.max_conns", 64)

	d("server.read_header_timeout", 5*time.Second)
	d("server.read_timeout", 15*time.Second)
	d("server.write_timeout", 15*time.Second)
	d("server.idle_timeout", 60*time.Second)
	d("server.shutdown_timeout", 5*time.Second)

	d("db.max_open_conns", 25)
	d("db.max_idle_conns", 25)
	d("db.conn_max_lifetime", 5*time.Minute)
	d("db.conn_max_idle_time", 5*time.Minute)

	d("redis.pool_size", 0)

	d("ratelimit.ip_per_min", 100)
	d("ratelimit.login_per_min", 10)
	d("ratelimit.market_per_min", 120)
	d("ratelimit.asr_per_min", 10)

	d("agent.qps", 2)
	d("agent.concurrent", 3)
	d("agent.daily_quota", 500)
	d("agent.proxy_concurrency", 64)
	d("agent.ws_max_conns", 512)
	d("agent.run_timeout", 120*time.Second)
	d("agent.ttft_timeout", 8*time.Second)
	d("agent.max_line_mb", 4)

	d("http.quote_timeout", 3*time.Second)
	d("http.search_timeout", 5*time.Second)
	d("http.wechat_timeout", 5*time.Second)
	d("http.wecom_timeout", 5*time.Second)
	d("http.asr_timeout", 20*time.Second)
	d("http.quote_cache_ttl", 30*time.Second)
	d("http.quote_fetch_limit", 5)
	d("http.quote_batch_size", 20)
	d("http.quote_mget_chunk", 100)

	d("hub.max_per_user", 5)
	d("hub.max_total", 2048)

	d("watchlist_max_items", 200)
}
