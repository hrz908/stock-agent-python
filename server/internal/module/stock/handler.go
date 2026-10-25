package stock

import (
	"log"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"

	"stock-agent-server/internal/deps"
	"stock-agent-server/internal/domain/stockcode"
	"stock-agent-server/internal/quote"
	"stock-agent-server/internal/resp"
)

// searchCacheTTL 搜索结果缓存 1 小时：股票代码不会快速增减，
// 同一 query 的相关性排序在长窗口内稳定，足以大幅降低单条 query 的请求量
const searchCacheTTL = time.Hour

// quoteCacheTTL 行情详情缓存 30 秒，与 quote 包的快照缓存约定一致
const quoteCacheTTL = 30 * time.Second

// maxLimit 单次最多返回 10 条，避免前端渲染过多、防恶意参数拖慢后端
const maxLimit = 10

// maxSearchRunes 搜索词长度上限：q 会拼进 Redis key（每个新 query 一条缓存），
// 不限制时几 KB 的 query 就能把内存和上游请求一起放大
const maxSearchRunes = 32

// SearchStock GET /api/v1/stocks/search?q=<query>&limit=5
// 股票模糊搜索，用于添加自选时的下拉联想。
// 走 Redis 缓存：搜索高频但实时性低（同 query 一小时内结果稳定）；
// 空 query 直接返回空数组，前端在防抖窗口过滤即可
func SearchStock(c *gin.Context) {
	q := strings.TrimSpace(c.Query("q"))
	if q == "" {
		resp.OK(c, []quote.SearchResult{})
		return
	}
	if utf8.RuneCountInString(q) > maxSearchRunes {
		resp.Fail(c, resp.CodeBadRequest, "搜索词过长")
		return
	}

	limit, err := strconv.Atoi(c.DefaultQuery("limit", "5"))
	if err != nil || limit <= 0 {
		limit = 5
	}
	if limit > maxLimit {
		limit = maxLimit
	}

	cacheKey := "stock:search:" + q + ":" + strconv.Itoa(limit)
	ctx := c.Request.Context()

	// 缓存命中直接吐 bytes，省一次序列化
	if deps.RDB != nil {
		if cached, err := deps.RDB.Get(ctx, cacheKey).Bytes(); err == nil && len(cached) > 0 {
			c.Header("X-Cache", "HIT")
			c.Data(200, "application/json; charset=utf-8", cached)
			return
		}
	}

	results := quote.Search(ctx, q, limit)
	source := "tencent"
	// 上游挂掉或确实无结果时用本地主数据兜底：搜索是加自选的第一道入口，
	// 第三方抽风不该让用户一只股票都搜不到。DB 出错时 SearchLocal 自己吞掉返回 nil
	if len(results) == 0 {
		results = SearchLocal(ctx, q, limit)
		source = "db"
	}
	if len(results) == 0 {
		source = "empty"
	}
	// 永远返回非 nil 切片，前端 map/foreach 不用判空
	if results == nil {
		results = []quote.SearchResult{}
	}
	// 序列化与缓存写同一份字节，保证缓存内容与真实响应逐字节一致
	body, err := resp.JSONBytes(resp.CodeOK, "ok", results)
	if err != nil {
		log.Printf("marshal search result failed: %v", err)
		resp.Fail(c, resp.CodeInternal, "序列化失败")
		return
	}

	// 空结果不写缓存：空往往来自第三方临时抽风或超时，
	// 缓存住会让搜索一小时什么都搜不到；写失败也不阻断，搜索是体验加分项
	if deps.RDB != nil && len(results) > 0 {
		_ = deps.RDB.Set(ctx, cacheKey, body, searchCacheTTL).Err()
	}

	c.Header("X-Cache", "MISS")
	c.Header("X-Search-Source", source)
	c.Data(200, "application/json; charset=utf-8", body)
}

// GetQuote GET /api/v1/stock/quote?code=600519
// 单只股票的完整行情快照：价格、涨跌、开高低、量额、状态与时间。
// 走 Redis 短缓存（30 秒）：详情页高频打开，同一只股票半分钟内的重复请求没必要打到第三方行情源
func GetQuote(c *gin.Context) {
	code, ok := stock.NormalizeCode(c.Query("code"))
	if !ok {
		resp.Fail(c, resp.CodeBadRequest, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字")
		return
	}

	cacheKey := "stock:quote:" + code
	ctx := c.Request.Context()

	if deps.RDB != nil {
		if cached, err := deps.RDB.Get(ctx, cacheKey).Bytes(); err == nil && len(cached) > 0 {
			c.Header("X-Cache", "HIT")
			c.Data(200, "application/json; charset=utf-8", cached)
			return
		}
	}

	detail, err := quote.FetchDetail(ctx, code)
	if err != nil || detail == nil {
		log.Printf("fetch quote detail failed: code=%s err=%v", code, err)
		resp.Fail(c, resp.CodeBadGateway, "行情获取失败，请稍后重试")
		return
	}

	body, err := resp.JSONBytes(resp.CodeOK, "ok", detail)
	if err != nil {
		log.Printf("marshal quote detail failed: %v", err)
		resp.Fail(c, resp.CodeInternal, "序列化失败")
		return
	}
	if deps.RDB != nil {
		_ = deps.RDB.Set(ctx, cacheKey, body, quoteCacheTTL).Err()
	}

	c.Header("X-Cache", "MISS")
	c.Data(200, "application/json; charset=utf-8", body)
}

// profileCacheTTL 主数据一天一更，缓存 1 小时足够，免得热门股票把库打热
const profileCacheTTL = time.Hour

// GetProfile GET /api/v1/stocks/profile?code=600519
// 股票主数据：名称 / 板块 / 行业 / 概念 / 上市退市，来自迁移 008 的三张表。
// 行情、估值不在这里——那是实时数据，走 /api/v1/stock/quote。
func GetProfile(c *gin.Context) {
	normalized, ok := stock.NormalizeCode(c.Query("code"))
	if !ok {
		resp.Fail(c, resp.CodeBadRequest, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字")
		return
	}
	code := normalized[2:]

	cacheKey := "stock:profile:" + code
	ctx := c.Request.Context()

	if deps.RDB != nil {
		if cached, err := deps.RDB.Get(ctx, cacheKey).Bytes(); err == nil && len(cached) > 0 {
			c.Header("X-Cache", "HIT")
			c.Data(200, "application/json; charset=utf-8", cached)
			return
		}
	}

	profile, err := FetchProfile(ctx, code)
	if err != nil {
		log.Printf("fetch stock profile failed: code=%s err=%v", code, err)
		resp.Fail(c, resp.CodeInternal, "主数据查询失败，请稍后重试")
		return
	}
	if profile == nil {
		// 表已建但没同步，或代码确实不在清单里；两种情况都按 404 处理，不猜
		resp.Fail(c, resp.CodeNotFound, "没有该股票的主数据，请先执行 sync-master")
		return
	}

	body, err := resp.JSONBytes(resp.CodeOK, "ok", profile)
	if err != nil {
		log.Printf("marshal stock profile failed: %v", err)
		resp.Fail(c, resp.CodeInternal, "序列化失败")
		return
	}
	if deps.RDB != nil {
		_ = deps.RDB.Set(ctx, cacheKey, body, profileCacheTTL).Err()
	}

	c.Header("X-Cache", "MISS")
	c.Data(200, "application/json; charset=utf-8", body)
}
