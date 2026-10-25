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
type tpl struct {
	tag     string
	display string
	ask     string
}

var (
	snapshotTemplates = []tpl{
		{
			tag:     "盘面",
			display: "快速看一下{name}（{code}）现在的盘面",
			ask:     "{name}（{code}）现在什么价？今天这波涨跌是什么逻辑，关键价位在哪",
		},
		{
			tag:     "盘面",
			display: "{name}今天这波涨跌背后是什么逻辑？",
			ask:     "{name}（{code}）今天这波涨跌背后是什么逻辑？结合量价和消息面说",
		},
		{
			tag:     "盘面",
			display: "用一句话总结{name}当前的多空博弈",
			ask:     "{name}（{code}）现在多空双方谁占优？用一句话总结，再补一句主要依据",
		},
	}

	// 休市（盘后 / 周末 / 盘前）不能复用「今天为什么涨」，改成复盘与下一个交易日
	closedSnapshotTemplates = []tpl{
		{
			tag:     "盘面",
			display: "复盘一下{name}（{code}）最近的走势",
			ask:     "{name}（{code}）最近几个交易日怎么走的？量价和资金面各有什么变化",
		},
		{
			tag:     "盘面",
			display: "{name}下一个交易日值得盯哪些价位？",
			ask:     "{name}（{code}）下一个交易日的关键价位和需要盯的信号是什么",
		},
	}

	capitalTemplates = []tpl{
		{
			tag:     "资金",
			display: "{name}最近主力资金和北向持仓有什么变化？",
			ask:     "拆解一下{name}（{code}）近5日的资金流向和筹码结构，主力资金和北向持仓有什么变化",
		},
		{
			tag:     "资金",
			display: "拆解一下{name}近5日的筹码结构",
			ask:     "{name}（{code}）近5日的资金流向和筹码结构怎么样？大单和散户分别在做什么",
		},
	}

	fundamentalTemplates = []tpl{
		{
			tag:     "基本面",
			display: "{name}最新财报的亮点和雷点分别是什么？",
			ask:     "{name}（{code}）最新财报的亮点和雷点分别是什么？关键科目各说一句",
		},
		{
			tag:     "基本面",
			display: "{name}的ROE和毛利在同行里算什么水平？",
			ask:     "对比{name}（{code}）的ROE、毛利率和同业水平，现在的估值贵不贵",
		},
	}

	technicalTemplates = []tpl{
		{
			tag:     "技术",
			display: "{name}现在的关键支撑位和阻力位在哪？",
			ask:     "{name}（{code}）现在的关键支撑位和阻力位在哪？结合量价说明",
		},
		{
			tag:     "技术",
			display: "从周线看{name}处于什么形态？",
			ask:     "从周线看{name}（{code}）处于什么形态？量价配合如何",
		},
	}

	riskTemplates = []tpl{
		{
			tag:     "风险",
			display: "{name}当前最大的三个风险点是什么？",
			ask:     "{name}（{code}）当前最大的三个风险点是什么？按重要性排序",
		},
		{
			tag:     "风险",
			display: "如果现在买{name}，止损和仓位怎么考虑？",
			ask:     "如果现在买{name}（{code}），止损位和仓位怎么考虑？给一个可执行的方案",
		},
	}

	watchTemplates = []tpl{
		{
			tag:     "观察",
			display: "把{name}加进观察池，给我后续的触发条件清单",
			ask:     "{name}（{code}）加进观察池，给出后续关键价位和触发条件清单",
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

// pickTemplateFor 在一批里挑「当前用得最少」的维度，同分随机。
// 一次给四条建议，若每条都走首选维度，盘后会四条全是基本面，像复读机；
// 这里用批内计数把它打散，但不会越过状态约束（大涨的票仍然只问盘面）。
func pickTemplateFor(c Candidate, p phase, used map[string]int) tpl {
	dims := eligibleDims(c, p)
	best := make([][]tpl, 0, len(dims))
	bestCount := -1
	for _, g := range dims {
		n := used[g[0].tag]
		switch {
		case bestCount < 0 || n < bestCount:
			bestCount = n
			best = append(best[:0], g)
		case n == bestCount:
			best = append(best, g)
		}
	}
	g := best[rand.Intn(len(best))]
	used[g[0].tag]++
	return randTpl(g)
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
