package quote

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/shopspring/decimal"

	"stock-agent-server/internal/domain/stockcode"
)

// KBar 一根 K 线。d 是原始时间串：分钟线形如 202610231457，日 / 周线形如 2026-10-23。
type KBar struct {
	D string          `json:"d"`
	O decimal.Decimal `json:"o"`
	H decimal.Decimal `json:"h"`
	L decimal.Decimal `json:"l"`
	C decimal.Decimal `json:"c"`
	V int64           `json:"v"`
}

// klinePeriods 支持的周期白名单。period 来自客户端，绝不能直接拼进上游 URL
// （拼串等于把上游地址交给调用方），先过白名单再取值
var klinePeriods = map[string]bool{"m1": true, "m5": true, "m15": true, "day": true, "week": true}

const (
	// klineMinuteURL 分钟 K 线。必须用 ifzq.gtimg.cn：换成 web.ifzq.gtimg.cn 会 301
	klineMinuteURL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param="
	// klineDailyURL 日 / 周 K 线（前复权）
	klineDailyURL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
)

// KlinePeriodSupported 周期是否在白名单内
func KlinePeriodSupported(period string) bool {
	return klinePeriods[period]
}

// FetchKlineBars 拉最近 limit 根 K 线，按时间升序返回。
// 日 / 周走前复权，分钟线本身无复权概念。
func FetchKlineBars(ctx context.Context, code, period string, limit int) ([]KBar, error) {
	if !klinePeriods[period] {
		return nil, fmt.Errorf("unsupported period %q", period)
	}
	norm, ok := stock.NormalizeCode(code)
	if !ok {
		return nil, fmt.Errorf("invalid stock code %q", code)
	}
	// 腾讯用小写市场前缀：SH600519 → sh600519
	symbol := strings.ToLower(norm)

	var url string
	if period == "day" || period == "week" {
		url = klineDailyURL + symbol + "," + period + ",,," + strconv.Itoa(limit) + ",qfq"
	} else {
		url = klineMinuteURL + symbol + "," + period + ",," + strconv.Itoa(limit)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("build kline request failed: %w", err)
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (stock-agent-miniapp)")

	resp, err := quoteHTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("request kline api failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("kline api http %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBodyBytes))
	if err != nil {
		return nil, fmt.Errorf("read kline body failed: %w", err)
	}
	// 这两个接口都返回 UTF-8 JSON（只有行情文本接口是 GBK），不需要转码
	return parseKlineBars(body, symbol, period)
}

// parseKlineBars 从 {"code":0,"data":{"sh600519":{"qfqday":[[...]],...}}} 里取 K 线数组
func parseKlineBars(body []byte, symbol, period string) ([]KBar, error) {
	var top struct {
		Code int                        `json:"code"`
		Data map[string]json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(body, &top); err != nil {
		return nil, fmt.Errorf("decode kline failed: %w", err)
	}
	raw, ok := top.Data[symbol]
	if !ok {
		return nil, fmt.Errorf("no kline data for %s", symbol)
	}

	var byKey map[string]json.RawMessage
	if err := json.Unmarshal(raw, &byKey); err != nil {
		return nil, fmt.Errorf("decode kline payload failed: %w", err)
	}

	// 前复权数据放在 qfqday / qfqweek，只有不复权时才是 day / week；
	// 分钟线的 key 就是周期本身
	keys := []string{period}
	switch period {
	case "day":
		keys = []string{"qfqday", "day"}
	case "week":
		keys = []string{"qfqweek", "week"}
	}

	for _, k := range keys {
		rows, ok := byKey[k]
		if !ok {
			continue
		}
		if bars := parseKlineRows(rows); len(bars) > 0 {
			return bars, nil
		}
	}
	return nil, fmt.Errorf("empty kline for %s %s", symbol, period)
}

// parseKlineRows 解析行数组。行格式：[时间, 开, 收, 高, 低, 量, ...]
// —— 是「开收高低」不是 OHLC 顺序，分钟线还会在量后面多塞一个 {} 和涨幅，多余列一律忽略
func parseKlineRows(raw json.RawMessage) []KBar {
	var rows [][]json.RawMessage
	if err := json.Unmarshal(raw, &rows); err != nil {
		return nil
	}

	out := make([]KBar, 0, len(rows))
	for _, r := range rows {
		if len(r) < 6 {
			continue
		}
		d := rawString(r[0])
		o, okO := rawDecimal(r[1])
		c, okC := rawDecimal(r[2])
		h, okH := rawDecimal(r[3])
		l, okL := rawDecimal(r[4])
		v, _ := rawDecimal(r[5])
		// 停牌 / 空窗时会出现空串价格，整行丢掉，别让 0 值 K 线把图上比例拉歪
		if d == "" || !okO || !okC || !okH || !okL {
			continue
		}
		out = append(out, KBar{D: d, O: o, H: h, L: l, C: c, V: v.IntPart()})
	}
	return out
}

func rawString(r json.RawMessage) string {
	var s string
	if err := json.Unmarshal(r, &s); err != nil {
		return ""
	}
	return s
}

func rawDecimal(r json.RawMessage) (decimal.Decimal, bool) {
	d, err := decimal.NewFromString(strings.TrimSpace(rawString(r)))
	if err != nil {
		return decimal.Zero, false
	}
	return d, true
}
