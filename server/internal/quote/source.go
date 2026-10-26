package quote

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/shopspring/decimal"
	"golang.org/x/sync/errgroup"
	"golang.org/x/text/encoding/simplifiedchinese"

	"stock-agent-server/internal/domain/stockcode"
	"stock-agent-server/internal/httpclient"
)

// quoteAPIBase 自建行情服务基地址（配置项 quote_api），为空时走内置腾讯源。
// 自建接口约定：GET {base}?code=SH600519，返回
//
//	{"code":"SH600519","name":"贵州茅台","price":"1705.30","change_percent":"1.23"}
//
// 数值一律用字符串传递，避免中间环节经 float64 丢精度。
// 只由 main 通过 Init 注入，不直接读环境变量，保证配置来源唯一。
var (
	quoteAPIBase string
	// quoteHTTP 复用连接：批量刷新时几十个请求打同一域名，每次新建 client 会连接无法复用、TIME_WAIT 堆积。
	// 行情属旁路数据，第三方卡住不能把刷新接口一起拖死（默认 3s 档）
	quoteHTTP = httpclient.New(3 * time.Second)
)

// Init 注入行情源与运行参数，main 启动时调用一次；空值/非法值保留默认档。
func Init(base string, quoteTimeout, searchTimeout, cacheTTL time.Duration, fetchLimit, batchSize, mgetChunk int) {
	quoteAPIBase = strings.TrimSpace(base)
	if quoteTimeout > 0 {
		quoteHTTP = httpclient.New(quoteTimeout)
	}
	if searchTimeout > 0 {
		searchHTTP = httpclient.New(searchTimeout)
	}
	if cacheTTL > 0 {
		ttl = cacheTTL
	}
	if fetchLimit > 0 {
		limit = fetchLimit
	}
	if batchSize > 0 {
		batchSizeP = batchSize
	}
	if mgetChunk > 0 {
		mgetChunkSize = mgetChunk
	}
}

const (
	// tencentQuoteURL 腾讯财经批量行情接口：支持一次查多只（q=sh600519,sz000001），
	// 返回 GBK 文本，字段以 ~ 分隔
	tencentQuoteURL = "https://qt.gtimg.cn/q="

	// maxBodyBytes 第三方响应读取上限，不限制则上游故障返回 GB 级内容会直接 OOM
	maxBodyBytes = 1 << 20
)

// 腾讯返回串中按 ~ 分隔的字段下标
const (
	tecName    = 1  // 股票名称
	tecCode    = 2  // 6 位代码
	tecPrice   = 3  // 最新价
	tecPercent = 32 // 涨跌幅（%）
)

// tencentVarRe 匹配腾讯响应里的 v_sh600519="..."；不能用 Index/LastIndex 截取，
// 响应出现多余引号时会截错范围。
var tencentVarRe = regexp.MustCompile(`v_[a-z0-9]+="([^"]*)"`)

func fetchStockQuote(ctx context.Context, code string) (*StockSnapshot, error) {
	snaps := fetchStockQuotes(ctx, []string{code})
	if len(snaps) == 0 {
		return nil, fmt.Errorf("no quote for %s", code)
	}
	return snaps[0], nil
}

// fetchStockQuotes 批量拉取，只返回解析成功的部分；
// 失败代码由调用方用「请求列表 - 成功集合」求差集得到。
func fetchStockQuotes(ctx context.Context, codes []string) []*StockSnapshot {
	if len(codes) == 0 {
		return nil
	}
	if quoteAPIBase != "" {
		return fetchFromCustomAPI(ctx, codes)
	}
	return fetchFromTencent(ctx, codes)
}

// fetchFromTencent 腾讯批量行情：一次请求拿回整批。
func fetchFromTencent(ctx context.Context, codes []string) []*StockSnapshot {
	symbols := make([]string, 0, len(codes))
	for _, c := range codes {
		// 拼 URL 前再校验一次代码：上游地址只应包含可信代码
		if _, ok := stock.NormalizeCode(c); !ok {
			log.Printf("skip invalid code in quote url: %q", c)
			continue
		}
		// 腾讯用小写市场前缀：SH600519 → sh600519
		symbols = append(symbols, strings.ToLower(c))
	}
	if len(symbols) == 0 {
		return nil
	}

	body, err := fetchTencentRaw(ctx, tencentQuoteURL+strings.Join(symbols, ","))
	if err != nil {
		log.Printf("fetch tencent quotes failed: %v", err)
		return nil
	}
	return parseTencentQuotes(body)
}

// fetchTencentRaw GET 腾讯接口并返回转码后的正文。
// 腾讯行情系接口一律返回 GBK，不转码中文股票名会乱码；
// 批量行情与单只详情（kline.go 的 FetchDetail）共用这一段。
func fetchTencentRaw(ctx context.Context, url string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", fmt.Errorf("build quote request failed: %w", err)
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (stock-agent-miniapp)")

	resp, err := quoteHTTP.Do(req)
	if err != nil {
		return "", fmt.Errorf("request quote api failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("quote api http %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(simplifiedchinese.GBK.NewDecoder().Reader(resp.Body), maxBodyBytes))
	if err != nil {
		return "", fmt.Errorf("decode quote body failed: %w", err)
	}
	return string(body), nil
}

// parseTencentQuotes 解析腾讯行情文本，每只股票一段：
// v_sh600519="1~贵州茅台~600519~1705.30~1700.00~...~0.31~...";
//
//	↑[0] ↑[1]     ↑[2]   ↑[3]     ↑[4]              ↑[32]
func parseTencentQuotes(body string) []*StockSnapshot {
	out := make([]*StockSnapshot, 0)

	for _, m := range tencentVarRe.FindAllStringSubmatch(body, -1) {
		line := m[0]
		eq := strings.Index(line, `="`)

		// 行首形如 v_sh600519，取 _ 之后的部分还原成 SH600519，用于回写 Code
		symbol := strings.ToUpper(line[:eq])
		if i := strings.Index(symbol, "_"); i >= 0 {
			symbol = symbol[i+1:]
		}

		fields := strings.Split(m[1], "~")
		if len(fields) <= tecPercent {
			continue
		}

		price, err := decimal.NewFromString(strings.TrimSpace(fields[tecPrice]))
		if err != nil {
			// 停牌 / 退市 / 代码不存在时价格字段为空，按无行情处理
			log.Printf("parse quote price failed: code=%s raw=%q", symbol, fields[tecPrice])
			continue
		}

		pct, err := decimal.NewFromString(strings.TrimSpace(fields[tecPercent]))
		if err != nil {
			pct = decimal.Zero
		}

		name := strings.TrimSpace(fields[tecName])
		if name == "" {
			name = symbol
		}

		out = append(out, &StockSnapshot{
			Code:          symbol,
			Name:          name,
			Price:         price,
			ChangePercent: pct,
		})
	}
	return out
}

// fetchFromCustomAPI 自建行情服务：批内并发。
// 逐只串行时一批 20 只 × 3s 超时最坏要 60s，慢一个量级。
func fetchFromCustomAPI(ctx context.Context, codes []string) []*StockSnapshot {
	var (
		mu      sync.Mutex
		out     = make([]*StockSnapshot, 0, len(codes))
		g, gctx = errgroup.WithContext(ctx)
	)
	g.SetLimit(limit)

	for _, code := range codes {
		code := code
		g.Go(func() error {
			snap, err := requestCustomQuote(gctx, code)
			if err != nil || snap == nil {
				log.Printf("fetch quote %s failed: %v", code, err)
				// 单只失败不取消同批其它请求：失败集合由调用方求差集得到
				return nil
			}
			mu.Lock()
			defer mu.Unlock()
			out = append(out, snap)
			return nil
		})
	}
	_ = g.Wait()
	return out
}

func requestCustomQuote(ctx context.Context, code string) (*StockSnapshot, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, quoteAPIBase, nil)
	if err != nil {
		return nil, err
	}
	q := req.URL.Query()
	q.Set("code", code)
	req.URL.RawQuery = q.Encode()

	resp, err := quoteHTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("quote api http %d", resp.StatusCode)
	}

	var s StockSnapshot
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxBodyBytes)).Decode(&s); err != nil {
		return nil, err
	}
	// 兜底：接口没给名称时用代码顶上，避免无名记录
	if strings.TrimSpace(s.Name) == "" {
		s.Name = code
	}
	s.Code = code
	return &s, nil
}
