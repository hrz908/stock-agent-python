package quote

import (
	"context"
	"log"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/errgroup"

	stock "stock-agent-server/internal/domain/stockcode"
	"stock-agent-server/internal/util"
)

// FetchDetails 批量拉完整行情（含成交额 / 换手率 / 量比），供热度池这类全池打分场景使用。
// 与 FetchQuotes 同一套「去重 → 分批 → 批间并发」策略；单批失败只丢该批，
// 调用方按「返回 map 里有没有这只」降级，不要指望它返回错误列表。
func FetchDetails(ctx context.Context, codes []string) map[string]*QuoteDetail {
	out := make(map[string]*QuoteDetail, len(codes))
	if len(codes) == 0 {
		return out
	}

	// 自建源只约定 4 个字段：成交额/换手率/量比全为零，
	// 调用方需要按「实际有值的信号」重新归一化权重，否则整池分数会被压平
	if quoteAPIBase != "" {
		snaps, _ := FetchQuotes(ctx, codes)
		status := marketStatus(time.Now())
		for code, s := range snaps {
			out[code] = &QuoteDetail{
				Code:          code,
				Name:          s.Name,
				Price:         s.Price,
				ChangePercent: s.ChangePercent,
				Status:        status,
			}
		}
		return out
	}

	// 拼 URL 前逐个校验代码：上游地址只应包含可信代码（与 fetchFromTencent 同一条底线）
	symbols := make([]string, 0, len(codes))
	for _, c := range util.Unique(codes) {
		if _, ok := stock.NormalizeCode(c); !ok {
			log.Printf("skip invalid code in detail url: %q", c)
			continue
		}
		symbols = append(symbols, strings.ToLower(c))
	}
	if len(symbols) == 0 {
		return out
	}

	var (
		mu      sync.Mutex
		g, gctx = errgroup.WithContext(ctx)
	)
	g.SetLimit(limit)

	for _, batch := range chunkStrings(symbols, batchSizeP) {
		batch := batch
		g.Go(func() error {
			body, err := fetchTencentRaw(gctx, tencentQuoteURL+strings.Join(batch, ","))
			if err != nil {
				log.Printf("fetch quote details failed: %v", err)
				// 不返回 error：errgroup 的取消会连带掐掉其它批次，
				// 一批超时不该让整池只拿到残缺数据
				return nil
			}
			details := parseTencentDetails(body)
			mu.Lock()
			defer mu.Unlock()
			for _, d := range details {
				out[d.Code] = d
			}
			return nil
		})
	}
	_ = g.Wait()
	return out
}
