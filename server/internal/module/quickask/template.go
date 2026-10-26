package quickask

import (
	"fmt"
	"math"
	"math/rand"
	"strings"
	"time"
)

// 市场阶段：语气跟着阶段走。盘后还问「今天为什么涨」是语境错位，
// 用户第一眼就会觉得这批建议是提前写死的。
type phase int

const (
	phasePreOpen phase = iota
	phaseTrading
	phaseAfterClose
	phaseWeekend
)

var shanghaiLoc = time.FixedZone("CST", 8*3600)

func phaseAt(now time.Time) phase {
	t := now.In(shanghaiLoc)
	switch t.Weekday() {
	case time.Saturday, time.Sunday:
		return phaseWeekend
	}
	hm := t.Hour()*60 + t.Minute()
	switch {
	case hm < 9*60+15:
		return phasePreOpen
	case hm < 15*60:
		return phaseTrading
	default:
		return phaseAfterClose
	}
}

// tpl 一条问句模板：display 是前端展示的一句话，ask 是真正发给 Agent 的完整问题。
// 拆成两份是因为展示要短（像随口一问），发出去的 query 要信息完整（带上代码与维度）。
//
// 写模板的两条硬约束（2026-10-26 按线上反馈重写）：
//  1. 只代入 {name}/{code} 就要通顺：模板不能假设「今天涨了」「刚出财报」这类前置事实，
//     否则套到任意一只票上都会出现「奇怪的股票名 + 句子」组合；
//  2. 必须能被现有取数工具答出来（括号里是对应工具）：行情/量能（get_quote）、
//     资金四档（get_money_flow）、估值（get_valuation）、财报与指标（get_financial_report /
//     get_financial_indicator）、近 15 个交易日走势（get_history_daily）、当日资讯
//     （get_stock_news）、机构研报（get_research_reports）、龙虎榜（get_top_list）、
//     同业对比（get_peer_compare）。北向持仓、筹码分布、周线/分钟线、买卖点建议都没有
//     对应数据源，一律不写进模板——问了只会答「取不到」。
//
// ask 里也不要写「用一句话总结，再补一句」这类提示词式要求：它会被原样显示在用户气泡里。
type tpl struct {
	tag     string
	display string
	ask     string
}

var (
	// 盘中异动（涨跌幅超 5%）：只问当下就能答的盘面
	snapshotTemplates = []tpl{
		{
			tag:     "盘面",
			display: "{name}现在什么价？",
			ask:     "{name}（{code}）现在什么价？涨跌幅、成交额和换手率分别是多少",
		},
		{
			tag:     "盘面",
			display: "{name}今天这波涨跌是什么原因？",
			ask:     "{name}（{code}）今天这波涨跌是什么原因？结合量价和今日消息说",
		},
		{
			tag:     "盘面",
			display: "{name}今天的最高价和最低价到过多少？",
			ask:     "{name}（{code}）今天的开盘价、最高价和最低价分别是多少",
		},
		{
			tag:     "盘面",
			display: "{name}今天的成交额和换手率是多少？",
			ask:     "{name}（{code}）今天的成交额、换手率和量比分别是多少",
		},
		{
			tag:     "盘面",
			display: "{name}现在的总市值是多少？",
			ask:     "{name}（{code}）现在的最新价、涨跌幅和总市值分别是多少",
		},
	}

	// 休市（盘后 / 周末 / 盘前）不能复用「今天为什么涨」，改成复盘
	closedSnapshotTemplates = []tpl{
		{
			tag:     "盘面",
			display: "复盘一下{name}最近的走势",
			ask:     "{name}（{code}）最近10个交易日的走势和量能怎么样",
		},
		{
			tag:     "盘面",
			display: "{name}现在离近期高点还有多远？",
			ask:     "{name}（{code}）近15个交易日的最高价和最低价是多少？现在处在区间什么位置",
		},
		{
			tag:     "盘面",
			display: "{name}最近成交量的变化怎么样？",
			ask:     "{name}（{code}）最近10个交易日的成交量有什么变化",
		},
		{
			tag:     "盘面",
			display: "{name}最近有没有放量？",
			ask:     "{name}（{code}）最近几个交易日有放量吗？量比和换手率是什么水平",
		},
	}

	capitalTemplates = []tpl{
		{
			tag:     "资金",
			display: "{name}最近主力资金在流入还是流出？",
			ask:     "{name}（{code}）最近5个交易日主力资金净流入情况怎么样",
		},
		{
			tag:     "资金",
			display: "{name}最近的超大单和大单在做什么？",
			ask:     "{name}（{code}）最新一个交易日的超大单、大单、中单、小单分别净流入多少",
		},
		{
			tag:     "资金",
			display: "{name}最近上过龙虎榜吗？",
			ask:     "{name}（{code}）最近上过龙虎榜吗？上榜的话席位买卖情况怎么样",
		},
		{
			tag:     "资金",
			display: "{name}的主力资金是连续流入吗？",
			ask:     "{name}（{code}）最近主力资金是连续净流入还是流出？连续了几天",
		},
	}

	fundamentalTemplates = []tpl{
		{
			tag:     "基本面",
			display: "{name}最新财报的营收和净利润是多少？",
			ask:     "{name}（{code}）最新一期财报的营业收入和净利润分别是多少？同比变化如何",
		},
		{
			tag:     "基本面",
			display: "{name}的毛利率和ROE是什么水平？",
			ask:     "{name}（{code}）最新的毛利率、净利率和ROE分别是多少",
		},
		{
			tag:     "基本面",
			display: "{name}最近有机构研报吗？",
			ask:     "{name}（{code}）最近有哪些机构研报？评级和目标价是多少",
		},
	}

	technicalTemplates = []tpl{
		{
			tag:     "技术",
			display: "{name}现在的关键支撑位和阻力位在哪？",
			ask:     "{name}（{code}）现在的关键支撑位和阻力位在哪？结合最近10个交易日的量价说",
		},
		{
			tag:     "技术",
			display: "{name}最近15个交易日的日K线怎么走的？",
			ask:     "{name}（{code}）最近15个交易日的日K线怎么走的？量能有什么变化",
		},
	}

	riskTemplates = []tpl{
		{
			tag:     "风险",
			display: "{name}最近有哪些需要留意的风险？",
			ask:     "{name}（{code}）从最近的消息和财务数据看，有哪些需要留意的风险",
		},
		{
			tag:     "风险",
			display: "{name}最近的股价波动主要受什么影响？",
			ask:     "{name}（{code}）最近股价波动主要受什么影响？资金面和消息面各说一点",
		},
	}

	watchTemplates = []tpl{
		{
			tag:     "观察",
			display: "{name}最近有哪些值得关注的信号？",
			ask:     "{name}（{code}）最近有哪些值得关注的信号？量价、资金和消息面各说一句",
		},
		{
			tag:     "观察",
			display: "{name}最近有需要跟踪的公告或事件吗？",
			ask:     "{name}（{code}）最近有需要跟踪的公告或事件吗？有的话说清是什么",
		},
	}
)

// eligibleDims 先看股票状态，再看市场阶段，给出该股票此刻可用的维度（顺序即优先级）。
// 状态决定「问什么」：大涨大跌问盘面、放量问资金、盘后问基本面，
// 横盘才轮到技术 / 观察 / 风险这类没有时效性的维度。
func eligibleDims(c Candidate, p phase) [][]tpl {
	if math.Abs(c.ChangePct) > 5 {
		if p == phaseTrading {
			return [][]tpl{snapshotTemplates}
		}
		return [][]tpl{closedSnapshotTemplates}
	}
	if c.VolumeRatio > 2 {
		return [][]tpl{capitalTemplates}
	}
	// 财报季暂不细分：网关侧没有财报日历，盘后统一偏基本面
	if p == phaseAfterClose || p == phaseWeekend {
		return [][]tpl{fundamentalTemplates, riskTemplates, watchTemplates}
	}
	return [][]tpl{technicalTemplates, watchTemplates, riskTemplates}
}

func pickTemplate(c Candidate, p phase) tpl {
	return randTpl(eligibleDims(c, p)[0])
}

// pickTemplateFor 在一批里挑「当前用得最少」的维度，同分随机；组内再优先挑没用过的模板。
// 一次给四条建议，若每条都走首选维度，盘后会四条全是基本面，像复读机；
// 维度打散之后仍可能抽到同一句（实测同屏出现两条「多空博弈」），所以模板也要计一次。
// 两者都只是批内偏好，不会越过状态约束（大涨的票仍然只问盘面）。
func pickTemplateFor(c Candidate, p phase, used map[string]int) tpl {
	dims := eligibleDims(c, p)
	best := make([][]tpl, 0, len(dims))
	bestCount := -1
	for _, g := range dims {
		n := used[tagKey(g[0].tag)]
		switch {
		case bestCount < 0 || n < bestCount:
			bestCount = n
			best = append(best[:0], g)
		case n == bestCount:
			best = append(best, g)
		}
	}
	g := best[rand.Intn(len(best))]
	used[tagKey(g[0].tag)]++
	t := pickLeastUsed(g, used)
	used[tplKey(t)]++
	return t
}

// 批内计数的 key 前缀：维度与模板共用一张 map，避免 buildAsk 再多一个参数。
const (
	tagKeyPrefix = "tag:"
	tplKeyPrefix = "tpl:"
)

func tagKey(tag string) string { return tagKeyPrefix + tag }
func tplKey(t tpl) string      { return tplKeyPrefix + t.display }

// pickLeastUsed 组内挑本批用得最少的模板，同分随机。
func pickLeastUsed(group []tpl, used map[string]int) tpl {
	pool := make([]tpl, 0, len(group))
	lowest := -1
	for _, t := range group {
		n := used[tplKey(t)]
		switch {
		case lowest < 0 || n < lowest:
			lowest = n
			pool = append(pool[:0], t)
		case n == lowest:
			pool = append(pool, t)
		}
	}
	return pool[rand.Intn(len(pool))]
}

func randTpl(list []tpl) tpl {
	return list[rand.Intn(len(list))]
}

// Ask 对外的单条建议问题。
type Ask struct {
	ID        string `json:"id"`
	Display   string `json:"display"`
	Query     string `json:"query"`
	StockCode string `json:"stock_code"`
	StockName string `json:"stock_name"`
	Tag       string `json:"tag"`
}

// buildAsk used 为批内维度计数，传 nil 表示单条生成（直接走状态首选维度）。
func buildAsk(c Candidate, p phase, now time.Time, used map[string]int) Ask {
	t := pickTemplate(c, p)
	if used != nil {
		t = pickTemplateFor(c, p, used)
	}
	code := digitsOf(c.Code)
	replacer := strings.NewReplacer("{name}", c.Name, "{code}", code)
	return Ask{
		ID:        fmt.Sprintf("qa_%s_%d", code, now.UnixNano()/int64(time.Millisecond)),
		Display:   replacer.Replace(t.display),
		Query:     replacer.Replace(t.ask),
		StockCode: code,
		StockName: c.Name,
		Tag:       t.tag,
	}
}

// digitsOf 取 6 位裸码：对外统一裸码，小程序 / Agent 两边的入口都认这种写法。
func digitsOf(code string) string {
	if i := strings.LastIndexFunc(code, func(r rune) bool { return r < '0' || r > '9' }); i >= 0 {
		code = code[i+1:]
	}
	if len(code) > 6 {
		code = code[len(code)-6:]
	}
	return code
}
