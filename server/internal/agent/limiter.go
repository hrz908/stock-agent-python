package agent

import (
	"context"
	"errors"
	"fmt"
	"log"
	"time"

	"github.com/redis/go-redis/v9"
)

// 三道闸门的错误值，供调用方映射 error code 回传前端。
// 限流是「拒绝本次请求」而非「断开连接」，故用结构化错误而非 panic / 断连。
var (
	ErrRateLimit         = errors.New("rate limit exceeded")
	ErrTooManyConcurrent = errors.New("too many concurrent sessions")
	ErrQuotaExceeded     = errors.New("daily quota exceeded")
)

// Limits 限流参数：QPS（令牌桶）、并发（计数）、日配额（计数 + 过期）。
type Limits struct {
	QPS        int // 每秒令牌数，兼作桶容量
	Concurrent int // 单用户并发运行数
	DailyQuota int // 单用户每日请求配额
}

// DefaultLimits 默认档位：2/s、3 并发、500/天。
var DefaultLimits = Limits{QPS: 2, Concurrent: 3, DailyQuota: 500}

// Limiter 基于 Redis 的限流器，计数落在 Redis，多实例网关天然共享配额。
type Limiter struct {
	r      *redis.Client
	limits Limits
}

func NewLimiter(r *redis.Client, limits Limits) *Limiter {
	return &Limiter{r: r, limits: limits}
}

// concKeyTTL 并发计数键的兜底过期时间：正常路径请求结束即 DECR 归还，
// 进程崩溃漏还时靠 TTL 清零，避免并发槽被永久占死；取值远大于单次运行上限（120s）即可。
const concKeyTTL = time.Hour

// qpsScript 令牌桶：按流逝时间补充令牌（不超容量），够扣则扣减返回 1，否则返回 0。
// 读-算-写在一段 Lua 内完成，杜绝并发竞态。
var qpsScript = redis.NewScript(`
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = 1

local data = redis.call('HMGET', key, 'tokens', 'last')
local tokens = tonumber(data[1]) or capacity
local last = tonumber(data[2]) or now

local elapsed = math.max(0, now - last)
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens < requested then
    redis.call('HMSET', key, 'tokens', tokens, 'last', now)
    redis.call('EXPIRE', key, 10)
    return 0
end

tokens = tokens - requested
redis.call('HMSET', key, 'tokens', tokens, 'last', now)
redis.call('EXPIRE', key, 10)
return 1
`)

// concScript 并发闸门：INCR 占槽，超限立即 DECR 回滚并返回 0，成功则刷新兜底 TTL。
// 用 Lua 保证「判断 + 回滚」原子，避免并发请求都越过上限。
var concScript = redis.NewScript(`
local count = redis.call('INCR', KEYS[1])
if count > tonumber(ARGV[1]) then
    redis.call('DECR', KEYS[1])
    return 0
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
`)

// quotaScript 日配额闸门：INCR 计数，首次（count==1）设置到当日 24:00 的过期，超限 DECR 回滚。
// 回滚保证被拒绝的请求不消耗配额，计数始终等于当日实际放行数。
var quotaScript = redis.NewScript(`
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
if count > tonumber(ARGV[2]) then
    redis.call('DECR', KEYS[1])
    return 0
end
return 1
`)

// Allow 依次过三道闸门：全部通过返回 nil，此时已占用一个并发槽，
// 调用方必须在请求结束后 defer ReleaseConcurrent 归还。
// Redis 故障导致无法判定时按 fail-open 放行——限流是旁路保护，
// 不应因 Redis 抖动打断业务，与 middleware 的限流策略一致。
func (l *Limiter) Allow(ctx context.Context, uid int64) error {
	// 闸门一：QPS 令牌桶
	qpsKey := fmt.Sprintf("rl:qps:%d", uid)
	nowSec := float64(time.Now().UnixMilli()) / 1000.0
	res, err := qpsScript.Run(ctx, l.r, []string{qpsKey}, l.limits.QPS, l.limits.QPS, nowSec).Int()
	if err != nil {
		log.Printf("agent limiter qps error (fail-open): %v", err)
	} else if res == 0 {
		return ErrRateLimit
	}

	// 闸门二：并发计数（成功后占槽，需配对归还）
	concKey := fmt.Sprintf("rl:conc:%d", uid)
	res, err = concScript.Run(ctx, l.r, []string{concKey}, l.limits.Concurrent, int(concKeyTTL.Seconds())).Int()
	if err != nil {
		log.Printf("agent limiter concurrent error (fail-open): %v", err)
	} else if res == 0 {
		return ErrTooManyConcurrent
	}

	// 闸门三：日配额；被拒时要回滚上面已占的并发槽，否则泄漏一个额度
	quotaKey := fmt.Sprintf("rl:quota:%d:%s", uid, time.Now().Format("2006-01-02"))
	res, err = quotaScript.Run(ctx, l.r, []string{quotaKey}, secondsToMidnight(), l.limits.DailyQuota).Int()
	if err != nil {
		log.Printf("agent limiter quota error (fail-open): %v", err)
	} else if res == 0 {
		l.r.Decr(ctx, concKey) // 回滚并发槽
		return ErrQuotaExceeded
	}

	return nil
}

// ReleaseConcurrent 归还一个并发槽。
// 用 WithoutCancel 脱离请求上下文：客户端断开后 ctx 往往已取消，
// 但归还必须执行，否则计数只增不减，用户会被自己的僵尸计数锁死。
func (l *Limiter) ReleaseConcurrent(ctx context.Context, uid int64) {
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 3*time.Second)
	defer cancel()
	if err := l.r.Decr(rctx, fmt.Sprintf("rl:conc:%d", uid)).Err(); err != nil {
		log.Printf("agent limiter release concurrent error: %v", err)
	}
}

// secondsToMidnight 距当日 24:00 的秒数，用作配额键的过期时间，
// 使计数跨天自动清零，无需定时清理任务。
func secondsToMidnight() int {
	now := time.Now()
	midnight := time.Date(now.Year(), now.Month(), now.Day()+1, 0, 0, 0, 0, now.Location())
	sec := int(midnight.Sub(now).Seconds())
	if sec <= 0 {
		sec = 1 // 边界保护：恰在午夜时至少给 1 秒，避免 EXPIRE 0 立即删除
	}
	return sec
}
