package quote

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/shopspring/decimal"

	"stock-agent-server/internal/domain/stockcode"
)

// QuoteDetail 单只股票的完整行情快照：比 StockSnapshot 多出开高低、涨跌额、量额、
// 换手率、市值、PE/PB、状态与时间，供行情详情页与 Agent 同业对比工具使用
// （全部来自腾讯当日快照，无需额外数据源）。
type QuoteDetail struct {
	Code          string          `json:"code"`
	Name          string          `json:"name"`
	Price         decimal.Decimal `json:"price"`
	Change        decimal.Decimal `json:"change"`
	ChangePercent decimal.Decimal `json:"change_percent"`
	Open          decimal.Decimal `json:"open"`
	High          decimal.Decimal `json:"high"`
	Low           decimal.Decimal `json:"low"`
	Volume        int64           `json:"volume"`       // 成交量（手）
	Amount        decimal.Decimal `json:"amount"`       // 成交额（万元）
	Turnover      decimal.Decimal `json:"turnover"`     // 换手率（%）
	VolumeRatio   decimal.Decimal `json:"volume_ratio"` // 量比（当日每分钟均量 / 过去5日每分钟均量）
	PE            decimal.Decimal `json:"pe"`           // 市盈率（腾讯口径，TTM）
	PB            decimal.Decimal `json:"pb"`           // 市净率
	TotalMv       decimal.Decimal `json:"total_mv"`     // 总市值（元；上游亿元×1e8）
	FreeMv        decimal.Decimal `json:"free_mv"`      // 流通市值（元；上游亿元×1e8）
	Status        string          `json:"status"`       // trading / closed
	Time          string          `json:"time"`         // 行情时间（腾讯返回的原始串）
}

// FetchDetail 拉单只股票的完整行情。
// 配置了自建行情源时降级复用 FetchOne（自建接口只约定 4 个字段），缺的量额等字段置零；
// 内置腾讯源则直接解析全字段。
func FetchDetail(ctx context.Context, code string) (*QuoteDetail, error) {
	if quoteAPIBase != "" {
		snap, err := FetchOne(ctx, code)
		if err != nil {
			return nil, err
		}
		return &QuoteDetail{
			Code:          snap.Code,
			Name:          snap.Name,
			Price:         snap.Price,
			ChangePercent: snap.ChangePercent,
			Status:        marketStatus(time.Now()),
		}, nil
	}

	if _, ok := stock.NormalizeCode(code); !ok {
		return nil, fmt.Errorf("invalid stock code %q", code)
	}

	body, err := fetchTencentRaw(ctx, tencentQuoteURL+strings.ToLower(code))
	if err != nil {
		return nil, err
	}
	// fetchTencentRaw 已完成 GBK → UTF-8 转码，这里按文本解析
	details := parseTencentDetails(body)
	if len(details) == 0 {
		return nil, fmt.Errorf("no quote detail for %s", code)
	}
	return details[0], nil
}

func parseDecimal(s string) decimal.Decimal {
	d, err := decimal.NewFromString(strings.TrimSpace(s))
	if err != nil {
		return decimal.Zero
	}
	return d
}

// 腾讯行情串中 QuoteDetail 用到的其余字段下标
// （tecName/tecCode/tecPrice/tecPercent 已在 source.go 定义；
// 下标已用 sh600519 真实响应核对：39=PE、46=PB、44=流通市值、45=总市值）
const (
	tecPrevClose   = 4  // 昨收
	tecOpen        = 5  // 今开
	tecVolume      = 6  // 成交量（手）
	tecChange      = 31 // 涨跌额
	tecHigh        = 33 // 最高
	tecLow         = 34 // 最低
	tecAmount      = 37 // 成交额（万元）
	tecTime        = 30 // 行情时间，形如 20261020150003
	tecTurnover    = 38 // 换手率（%）
	tecPE          = 39 // 市盈率（TTM）
	tecFreeMv      = 44 // 流通市值（亿元）
	tecTotalMv     = 45 // 总市值（亿元）
	tecPB          = 46 // 市净率
	tecVolumeRatio = 49 // 量比（49 之后是委差/均价等，本结构不用）
)

// parseTencentDetails 解析腾讯行情文本为完整快照，字段布局同 parseTencentQuotes
func parseTencentDetails(body string) []*QuoteDetail {
	out := make([]*QuoteDetail, 0)

	for _, m := range tencentVarRe.FindAllStringSubmatch(body, -1) {
		line := m[0]
		eq := strings.Index(line, `="`)

		symbol := strings.ToUpper(line[:eq])
		if i := strings.Index(symbol, "_"); i >= 0 {
			symbol = symbol[i+1:]
		}

		fields := strings.Split(m[1], "~")
		if len(fields) <= tecTotalMv {
			continue
		}

		price := parseDecimal(fields[tecPrice])
		if len(strings.TrimSpace(fields[tecPrice])) == 0 {
			// 停牌 / 退市 / 代码不存在时价格字段为空，按无行情处理
			log.Printf("parse quote detail failed: code=%s raw=%q", symbol, fields[tecPrice])
			continue
		}

		name := strings.TrimSpace(fields[tecName])
		if name == "" {
			name = symbol
		}

		// 量比在部分品种（指数 / 停牌股）上缺字段，长度不够时留零，不影响其它字段
		volumeRatio := decimal.Zero
		if len(fields) > tecVolumeRatio {
			volumeRatio = parseDecimal(fields[tecVolumeRatio])
		}

		out = append(out, &QuoteDetail{
			Code:          symbol,
			Name:          name,
			Price:         price,
			Change:        parseDecimal(fields[tecChange]),
			ChangePercent: parseDecimal(fields[tecPercent]),
			Open:          parseDecimal(fields[tecOpen]),
			High:          parseDecimal(fields[tecHigh]),
			Low:           parseDecimal(fields[tecLow]),
			Volume:        int64(parseDecimal(fields[tecVolume]).IntPart()),
			Amount:        parseDecimal(fields[tecAmount]),
			Turnover:      parseDecimal(fields[tecTurnover]),
			VolumeRatio:   volumeRatio,
			PE:            parseDecimal(fields[tecPE]),
			PB:            parseDecimal(fields[tecPB]),
			TotalMv:       parseDecimal(fields[tecTotalMv]).Mul(decimal.NewFromInt(100000000)),
			FreeMv:        parseDecimal(fields[tecFreeMv]).Mul(decimal.NewFromInt(100000000)),
			Status:        marketStatus(time.Now()),
			Time:          formatQuoteTime(strings.TrimSpace(fields[tecTime])),
		})
	}
	return out
}

// formatQuoteTime 把紧凑时间串（20261020150003）转成可读格式；长度不符时原样返回，不做臆测。
func formatQuoteTime(raw string) string {
	if len(raw) != 14 {
		return raw
	}
	return raw[0:4] + "-" + raw[4:6] + "-" + raw[6:8] + " " + raw[8:10] + ":" + raw[10:12] + ":" + raw[12:14]
}

// shanghaiLoc A 股行情时间基准时区
var shanghaiLoc = time.FixedZone("CST", 8*3600)

// marketStatus 按交易时段粗判市场状态（周一至五 9:30-11:30 / 13:00-15:00），不含节假日日历：
// 详情页只需区分「是否交易时段」，不值得为此维护节假日表。
func marketStatus(now time.Time) string {
	t := now.In(shanghaiLoc)
	if wd := t.Weekday(); wd == time.Saturday || wd == time.Sunday {
		return "closed"
	}
	hm := t.Hour()*60 + t.Minute()
	morning := hm >= 9*60+30 && hm < 11*60+30
	afternoon := hm >= 13*60 && hm < 15*60
	if morning || afternoon {
		return "trading"
	}
	return "closed"
}
