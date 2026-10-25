package quickask

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	stock "stock-agent-server/internal/domain/stockcode"
	"stock-agent-server/internal/httpclient"
)

// 候选池主路：新浪「沪深 A 股按成交额降序」榜单。
//
// 为什么不用全量行情自己排：全市场 5000+ 支要 200 多次批量请求，候选池每 10 分钟
// 重建一次就把上游当成扫描器用；而「成交额前 200」本身就是热度过滤结果，
// 正好对应设计里的「候选池」这一层，2 次请求就能拿到。
const sinaRankURL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page=%d&num=100&sort=amount&asc=0&node=hs_a"

// rankClient 单独一个客户端：榜单接口偶尔慢，不该拖住其它模块的默认超时
var rankClient = httpclient.New(8 * time.Second)

type rankItem struct {
	Symbol       string  `json:"symbol"` // sz300308
	Code         string  `json:"code"`   // 300308
	Name         string  `json:"name"`   // 中际旭创
	ChangePct    float64 `json:"changepercent"`
	Amount       float64 `json:"amount"`        // 成交额（元）
	TurnoverRate float64 `json:"turnoverratio"` // 换手率（%）
}

// loadRankUniverse 取成交额前 200（2 页 × 100）。任何一页失败就用手上已有的，
// 两页都失败返回空，由调用方退回「自选 + 常青」保底。
func loadRankUniverse(ctx context.Context) []Candidate {
	out := make([]Candidate, 0, maxUniverse)
	seen := make(map[string]bool, maxUniverse)

	for page := 1; page <= 2; page++ {
		items, err := fetchRankPage(ctx, page)
		if err != nil {
			log.Printf("fetch rank page %d failed: %v", page, err)
			break
		}
		for _, it := range items {
			code, ok := stock.NormalizeCode(it.Symbol)
			if !ok || it.Name == "" || seen[code] {
				continue
			}
			seen[code] = true
			out = append(out, Candidate{
				Code:      code,
				Name:      it.Name,
				ChangePct: it.ChangePct,
				// 元 → 万元，与腾讯 amount 口径对齐，否则同池内分位计算会串
				Amount:   it.Amount / 1e4,
				Turnover: it.TurnoverRate,
			})
			if len(out) >= maxUniverse {
				return out
			}
		}
	}
	return out
}

func fetchRankPage(ctx context.Context, page int) ([]rankItem, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf(sinaRankURL, page), nil)
	if err != nil {
		return nil, err
	}
	// 新浪榜单对无 Referer 的请求会返回空数组，UA 也必须是浏览器口径
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
	req.Header.Set("Referer", "https://finance.sina.com.cn")

	res, err := rankClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected status %d", res.StatusCode)
	}
	// 100 条 ≈ 30KB，2MB 上限足够，避免异常响应把内存吃满
	body, err := io.ReadAll(io.LimitReader(res.Body, 2<<20))
	if err != nil {
		return nil, err
	}
	return parseRankItems(body)
}

func parseRankItems(body []byte) ([]rankItem, error) {
	var items []rankItem
	if err := json.Unmarshal(body, &items); err != nil {
		return nil, err
	}
	return items, nil
}
