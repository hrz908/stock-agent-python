package quickask

import (
	"fmt"
	"log"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"stock-agent-server/internal/deps"
	stock "stock-agent-server/internal/domain/stockcode"
	"stock-agent-server/internal/resp"
)

const (
	defaultCount  = 4
	minCount      = 3
	maxCount      = 6
	maxSessionLen = 64

	// respTTL 同一会话 30 秒内重复请求直接复用结果：首屏狂点刷新时，
	// 每次重抽只会让建议看起来在乱跳
	respTTL = 30 * time.Second
)

type listResponse struct {
	QuickAsks   []Ask  `json:"quick_asks"`
	GeneratedAt string `json:"generated_at"`
	PoolSize    int    `json:"pool_size"`
	Source      string `json:"source"`
}

// List GET /api/v1/quick-asks?count=4&session_id=xxx
// 首屏引导问题：候选池 → 会话去重 → 热度加权抽样 → 按状态选模板拼问句。
func List(c *gin.Context) {
	uid, ok := userID(c)
	if !ok {
		resp.Fail(c, resp.CodeUnauthorized, "登录已失效，请重试")
		return
	}

	count := clampCount(c.Query("count"))
	sid := sanitizeSession(c.Query("session_id"))
	ctx := c.Request.Context()

	cacheKey := fmt.Sprintf("quickask:resp:%d:%s:%d", uid, sid, count)
	if deps.RDB != nil {
		if cached, err := deps.RDB.Get(ctx, cacheKey).Bytes(); err == nil && len(cached) > 0 {
			c.Header("X-Cache", "HIT")
			c.Data(200, "application/json; charset=utf-8", cached)
			return
		}
	}

	pool, source := loadPool(ctx)
	if len(pool) == 0 {
		pool, source = evergreenPool(), "fallback"
	}

	seen := readSeen(ctx, uid, sid)
	if countUnseen(pool, seen) < count {
		// 池子快被这个会话看完了：直接开新一轮，否则后面每次都走
		// 「不够就退回全池」的分支，建议会在这几条里反复打转
		clearSeen(ctx, uid, sid)
		seen = map[string]bool{}
	}

	picked := pickForSession(pool, count, seen, readLast(ctx, uid, sid))
	// 池子比 count 还小（极端降级）时用常青股补足：宁可多给一条，也不要首屏只剩一两条
	if len(picked) < count {
		for _, c := range evergreenPool() {
			if len(picked) >= count {
				break
			}
			if !containsCode(picked, c.Code) {
				picked = append(picked, c)
			}
		}
	}

	now := time.Now()
	marketPhase := phaseAt(now)
	asks := make([]Ask, 0, len(picked))
	codes := make([]string, 0, len(picked))
	usedDims := make(map[string]int, 3)
	for _, cand := range picked {
		asks = append(asks, buildAsk(cand, marketPhase, now, usedDims))
		codes = append(codes, cand.Code)
	}

	body, err := resp.JSONBytes(resp.CodeOK, "ok", listResponse{
		QuickAsks:   asks,
		GeneratedAt: now.In(shanghaiLoc).Format(time.RFC3339),
		PoolSize:    len(pool),
		Source:      source,
	})
	if err != nil {
		log.Printf("marshal quick asks failed: %v", err)
		resp.Fail(c, resp.CodeInternal, "序列化失败")
		return
	}

	// 去重状态先落，再写响应缓存：缓存只有 30 秒，过期后的下一轮
	// 必须能看见这一轮已经用过的 code，否则刷新会立刻撞回同一批股票
	markSeen(ctx, uid, sid, codes)
	saveLast(ctx, uid, sid, codes)
	if deps.RDB != nil {
		if err := deps.RDB.Set(ctx, cacheKey, body, respTTL).Err(); err != nil {
			log.Printf("cache quick asks failed: %v", err)
		}
	}

	c.Header("X-Cache", "MISS")
	c.Data(200, "application/json; charset=utf-8", body)
}

type clickRequest struct {
	StockCode string `json:"stock_code"`
}

// Click POST /api/v1/quick-asks/click
// 只做一件事：把点击记进行为热度。失败不影响用户发问，前端也不等它的结果。
func Click(c *gin.Context) {
	var req clickRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		resp.Fail(c, resp.CodeBadRequest, "参数格式不正确")
		return
	}
	code, ok := stock.NormalizeCode(req.StockCode)
	if !ok {
		resp.Fail(c, resp.CodeBadRequest, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字")
		return
	}
	bumpAskHeat(c.Request.Context(), code)
	resp.OK(c, gin.H{"ok": true})
}

func userID(c *gin.Context) (int64, bool) {
	v, ok := c.Get("user_id")
	if !ok {
		return 0, false
	}
	id, ok := v.(int64)
	return id, ok
}

func clampCount(raw string) int {
	n, err := strconv.Atoi(raw)
	if err != nil {
		return defaultCount
	}
	if n < minCount {
		return minCount
	}
	if n > maxCount {
		return maxCount
	}
	return n
}

// sanitizeSession session_id 会拼进 Redis key：只放行字母数字与 - _，
// 超长或含其它字符一律退回「按用户」维度，避免被拿来随手造 key。
func sanitizeSession(raw string) string {
	if raw == "" || len(raw) > maxSessionLen {
		return ""
	}
	for _, r := range raw {
		switch {
		case r >= 'a' && r <= 'z':
		case r >= 'A' && r <= 'Z':
		case r >= '0' && r <= '9':
		case r == '-' || r == '_':
		default:
			return ""
		}
	}
	return raw
}
