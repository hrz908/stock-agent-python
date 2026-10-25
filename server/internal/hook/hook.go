package hook

import (
	"context"
	"log"
	"sync"
)

// QuoteUpdatedFunc 回调签名：某用户的一批行情刷新完成后触发。
type QuoteUpdatedFunc func(userID int64, ctx context.Context)

var (
	mu           sync.Mutex
	quoteUpdated []QuoteUpdatedFunc
)

// RegisterQuoteUpdated 注册「行情刷新完成」钩子。
// 用钩子而非直接调用，让业务模块零依赖：刷新方只管刷新，关心方自己注册。
func RegisterQuoteUpdated(fn QuoteUpdatedFunc) {
	mu.Lock()
	defer mu.Unlock()
	quoteUpdated = append(quoteUpdated, fn)
}

// EmitQuoteUpdatedAsync 异步触发：提醒检查含 DB 查询、Redis SetNX、企业微信推送，
// 同步跑完会明显拖慢 refresh 的响应。
// 用 WithoutCancel 脱离请求 ctx，否则请求结束 ctx 被取消会让检查中途死掉。
func EmitQuoteUpdatedAsync(userID int64, ctx context.Context) {
	fns := funcs()
	if len(fns) == 0 {
		return
	}
	go func() {
		bg := context.WithoutCancel(ctx)
		for _, fn := range fns {
			safeRun(fn, userID, bg)
		}
	}()
}

// funcs 拷贝一份再遍历，避免边遍历边注册
func funcs() []QuoteUpdatedFunc {
	mu.Lock()
	defer mu.Unlock()
	return append([]QuoteUpdatedFunc(nil), quoteUpdated...)
}

// safeRun 隔离单个钩子的 panic，避免一个模块拖垮整条刷新链路
func safeRun(fn QuoteUpdatedFunc, userID int64, ctx context.Context) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("quote updated hook panic: %v", r)
		}
	}()
	fn(userID, ctx)
}
