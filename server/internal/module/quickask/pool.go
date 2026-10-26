package quickask

import (
	"context"
	"log"
	"math"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"

	"stock-agent-server/internal/deps"
	stock "stock-agent-server/internal/domain/stockcode"
	"stock-agent-server/internal/quote"
)

// 候选池只存 Redis，不落库：热度分是 10 分钟量级的中间结果，
// 落库既多一张表，又要额外处理「上游没数」时的脏读。
const (
	poolKey        = "stock:hot:pool"      // ZSET：score = 热度分，member = 5 段股票信息
	poolTempPrefix = "stock:hot:pool:tmp:" // 重建期间的临时键，写完整批后 RENAME 原子替换
	askHeatKey     = "stock:ask:heat"      // ZSET：站内 Quick Ask 点击次数，作为行为热度
	poolTTL        = 10 * time.Minute
	askHeatTTL     = 7 * 24 * time.Hour

	maxPoolSize = 200 // 候选池上限：再大就会混进没人认识的冷门票
	maxUniverse = 200 // 参与打分的股票上限：200 只 ≈ 10 次批量行情请求
)

// 打分权重（对齐设计文档）：只统计网关侧真正拿得到的信号。
// 新闻热度 / 板块资金流 / 龙虎榜与北向持仓在网关没有数据源，权重不按 0 计入，
// 而是把剩余权重按比例摊给可用信号——否则整池分数会被系统性压平，
// 「热度加权」退化成「接近均匀随机」。
const (
	weightChangePct = 0.25
	weightAmount    = 0.20
	weightTurnover  = 0.15
	weightBehavior  = 0.15
	weightAvailable = weightChangePct + weightAmount + weightTurnover + weightBehavior
)

// Candidate 池子里的一只股票。Amount / Turnover / Heat 只参与打分，不写 Redis：
// 池子读取方（抽样 + 模板）只需要名称、板块、涨跌幅和量比。
type Candidate struct {
	Code        string // 带市场前缀：SH600519
	Name        string
	Sector      string
	ChangePct   float64
	VolumeRatio float64
	Score       float64
	Amount      float64
	Turnover    float64
	Heat        float64
}

// evergreen 冷启动与保底清单：候选池为空（凌晨、主数据未同步、Redis 挂了）时
// 也要有像样的股票可问。同时它给零行为数据的新站一个最低热度，
// 避免「全市场按热度排序」在数据稀疏时退化成按代码排序。
var evergreen = []Candidate{
	{Code: "SH600519", Name: "贵州茅台", Sector: "食品饮料"},
	{Code: "SZ300750", Name: "宁德时代", Sector: "电力设备"},
	{Code: "SZ002594", Name: "比亚迪", Sector: "汽车"},
	{Code: "SH600036", Name: "招商银行", Sector: "银行"},
	{Code: "SZ300059", Name: "东方财富", Sector: "非银金融"},
	{Code: "SZ300308", Name: "中际旭创", Sector: "通信"},
	{Code: "SH601318", Name: "中国平安", Sector: "非银金融"},
	{Code: "SZ000858", Name: "五粮液", Sector: "食品饮料"},
	{Code: "SH600900", Name: "长江电力", Sector: "公用事业"},
	{Code: "SH601899", Name: "紫金矿业", Sector: "有色金属"},
	{Code: "SZ002475", Name: "立讯精密", Sector: "电子"},
	{Code: "SZ000333", Name: "美的集团", Sector: "家用电器"},
	{Code: "SH688981", Name: "中芯国际", Sector: "电子"},
	{Code: "SH601012", Name: "隆基绿能", Sector: "电力设备"},
	{Code: "SH600030", Name: "中信证券", Sector: "非银金融"},
	{Code: "SZ002415", Name: "海康威视", Sector: "计算机"},
	{Code: "SH603259", Name: "药明康德", Sector: "医药生物"},
	{Code: "SZ000001", Name: "平安银行", Sector: "银行"},
	{Code: "SH601088", Name: "中国神华", Sector: "煤炭"},
	{Code: "SH600276", Name: "恒瑞医药", Sector: "医药生物"},
	{Code: "SZ000651", Name: "格力电器", Sector: "家用电器"},
	{Code: "SH601857", Name: "中国石油", Sector: "石油石化"},
	{Code: "SH600887", Name: "伊利股份", Sector: "食品饮料"},
	{Code: "SZ002230", Name: "科大讯飞", Sector: "计算机"},
	{Code: "SH601668", Name: "中国建筑", Sector: "建筑装饰"},
	{Code: "SZ300124", Name: "汇川技术", Sector: "机械设备"},
	{Code: "SH600585", Name: "海螺水泥", Sector: "建筑材料"},
	{Code: "SZ002304", Name: "洋河股份", Sector: "食品饮料"},
}

// buildMu 防止同进程内并发重建：池子过期那一瞬间涌进来的请求不该各建一遍
var buildMu sync.Mutex

// memPool 仅在 Redis 不可用时兜底，行为与 ZSET 一致但不跨实例
var (
	memPool []Candidate
	memAt   time.Time
)

// loadPool 读候选池；为空（过期 / 首次 / Redis 被清）时惰性重建并回写。
// 仓库约定不使用 cron，重建只能由请求触发；代价被 TTL 摊平，10 分钟内只有第一个请求变慢。
func loadPool(ctx context.Context) ([]Candidate, string) {
	if pool := readPool(ctx); len(pool) > 0 {
		return pool, "redis"
	}

	buildMu.Lock()
	defer buildMu.Unlock()
	// 等锁期间别的请求可能已经建好，再读一次，避免同一轮重复打上游
	if pool := readPool(ctx); len(pool) > 0 {
		return pool, "redis"
	}

	// Redis 不可用（未部署 / 连接失败）时用进程内缓存兜住 TTL，
	// 否则每个首屏请求都会重新拉一轮行情，等于把上游当本地缓存用
	if deps.RDB == nil && len(memPool) > 0 && time.Since(memAt) < poolTTL {
		return memPool, "memory"
	}

	pool := buildPool(ctx)
	if len(pool) == 0 {
		return evergreenPool(), "fallback"
	}
	if deps.RDB == nil {
		memPool, memAt = pool, time.Now()
	}
	writePool(ctx, pool)
	return pool, "built"
}

func readPool(ctx context.Context) []Candidate {
	if deps.RDB == nil {
		return nil
	}
	res, err := deps.RDB.ZRevRangeWithScores(ctx, poolKey, 0, maxPoolSize-1).Result()
	if err != nil {
		// Redis 抖动不该让首屏建议整个消失，调用方会退回 fallback
		log.Printf("read hot pool failed: %v", err)
		return nil
	}
	out := make([]Candidate, 0, len(res))
	for _, z := range res {
		member, ok := z.Member.(string)
		if !ok {
			continue
		}
		c, ok := decodeCandidate(member, z.Score)
		if !ok {
			continue
		}
		out = append(out, c)
	}
	return out
}

func writePool(ctx context.Context, pool []Candidate) {
	if deps.RDB == nil || len(pool) == 0 {
		return
	}
	tmp := poolTempPrefix + strconv.FormatInt(time.Now().UnixNano(), 10)
	pipe := deps.RDB.Pipeline()
	pipe.Del(ctx, tmp)
	for _, c := range pool {
		pipe.ZAdd(ctx, tmp, redis.Z{Score: c.Score, Member: c.encode()})
	}
	pipe.Expire(ctx, tmp, poolTTL)
	// RENAME 原子替换：读方不会撞上「旧池已删、新池没写完」的空窗
	pipe.Rename(ctx, tmp, poolKey)
	if _, err := pipe.Exec(ctx); err != nil {
		log.Printf("write hot pool failed: %v", err)
	}
}

// buildPool 打分并返回按热度降序的候选池；DB / 上游全挂时返回空，由调用方兜底。
func buildPool(ctx context.Context) []Candidate {
	universe, heat := loadUniverse(ctx)
	if len(universe) == 0 {
		return nil
	}

	details := quote.FetchDetails(ctx, candidateCodes(universe))
	if len(details) == 0 {
		log.Printf("build hot pool: no quote details, universe=%d", len(universe))
		return nil
	}

	scored := make([]Candidate, 0, len(universe))
	for _, c := range universe {
		d, ok := details[c.Code]
		if !ok || d.Price.IsZero() {
			// 停牌 / 退市 / 上游漏返回：价格为零的数据进池只会生成「现在什么价」这种烂问题
			continue
		}
		c.Name = pickName(c.Name, d.Name)
		c.ChangePct, _ = d.ChangePercent.Float64()
		c.VolumeRatio, _ = d.VolumeRatio.Float64()
		c.Amount, _ = d.Amount.Float64()
		c.Turnover, _ = d.Turnover.Float64()
		c.Heat = heat[c.Code]
		c.Sector = strings.TrimSpace(c.Sector)
		scored = append(scored, c)
	}

	score(scored)
	sort.SliceStable(scored, func(i, j int) bool {
		if scored[i].Score != scored[j].Score {
			return scored[i].Score > scored[j].Score
		}
		return scored[i].Code < scored[j].Code
	})
	if len(scored) > maxPoolSize {
		scored = scored[:maxPoolSize]
	}
	return scored
}

// loadUniverse 组装打分前的候选清单：成交额榜前 200 为主路，
// 榜单不可用时退回「站内自选 + 常青股」。
//
// 绝不能「按代码顺序取前 200」：零热度股票的排序会退化成字典序，
// BJ920xxx 整段排在最前，候选池会被北交所小票占满（+30% 的陌生票直接进首屏）。
func loadUniverse(ctx context.Context) ([]Candidate, map[string]float64) {
	heat := loadAskHeat(ctx)
	for code, n := range loadWatchlistHeat(ctx) {
		heat[code] += n
	}
	meta := loadMeta(ctx)
	now := time.Now()

	ranked := loadRankUniverse(ctx)
	if len(ranked) < 20 {
		// 榜单抓不到（网络 / 接口改版）：只用有人关注过的 + 常青股，
		// 宁可池子小，也不要拿零热度的陌生代码凑数
		ranked = watchlistUniverse(meta, heat)
	}

	out := make([]Candidate, 0, len(ranked))
	for _, c := range ranked {
		m, ok := meta[c.Code]
		if ok {
			// ST / 已退市硬过滤：让用户点开一支 ST 股的「风险点」没有产品价值
			if m.IsST || m.Status != "listed" || freshListing(m.ListDate, now) {
				continue
			}
			c.Name = pickName(m.Name, c.Name)
			c.Sector = strings.TrimSpace(m.Sector)
			// 除权除息临时简称（XD/XR/DR）不进池：文案会变成「XD澜起科现在什么价？」
			if exRightsMarked(c.Name) {
				continue
			}
		} else if strings.HasPrefix(c.Code, "BJ") {
			// 榜单本身不含北交所；没有主数据佐证时不要把 BJ 小票放进来
			continue
		}
		c.Heat = heat[c.Code]
		out = append(out, c)
	}
	if len(out) > maxUniverse {
		out = out[:maxUniverse]
	}
	return out, heat
}

// watchlistUniverse 榜单失败时的保底：常青股 + 有行为热度（自选加入 / 点击）的股票。
// 不掺零热度的陌生代码——按代码补满 200 只会把北交所小票顶到前面。
func watchlistUniverse(meta map[string]metaRow, heat map[string]float64) []Candidate {
	out := make([]Candidate, 0, len(heat)+len(evergreen))
	seen := make(map[string]bool, len(evergreen)+len(heat))

	add := func(code, name, sector string, h float64) {
		if seen[code] {
			return
		}
		if m, ok := meta[code]; ok {
			if m.Status != "listed" || m.IsST {
				return
			}
			name = pickName(m.Name, name)
			sector = pickName(sector, strings.TrimSpace(m.Sector))
		}
		if name == "" || exRightsMarked(name) {
			return
		}
		seen[code] = true
		out = append(out, Candidate{Code: code, Name: name, Sector: sector, Heat: h})
	}

	for _, c := range evergreen {
		add(c.Code, c.Name, c.Sector, 1+heat[c.Code])
	}
	for code, h := range heat {
		if h < 1 {
			continue
		}
		add(code, "", "", h)
	}

	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Heat != out[j].Heat {
			return out[i].Heat > out[j].Heat
		}
		return out[i].Code < out[j].Code
	})
	if len(out) > maxUniverse {
		out = out[:maxUniverse]
	}
	return out
}

func candidateCodes(list []Candidate) []string {
	out := make([]string, 0, len(list))
	for _, c := range list {
		out = append(out, c.Code)
	}
	return out
}

func evergreenPool() []Candidate {
	out := make([]Candidate, 0, len(evergreen))
	for _, c := range evergreen {
		c.Score = 100
		out = append(out, c)
	}
	return out
}

// score 在池内做相对归一化：涨跌幅取「偏离池均值」的绝对值，
// 成交额 / 换手率 / 行为热度取分位。绝对阈值在牛熊市里都会失真，相对排序更稳。
func score(list []Candidate) {
	n := len(list)
	if n == 0 {
		return
	}

	mean := 0.0
	for _, c := range list {
		mean += c.ChangePct
	}
	mean /= float64(n)

	maxDev := 0.0
	deviations := make([]float64, n)
	for i, c := range list {
		deviations[i] = math.Abs(c.ChangePct - mean)
		if deviations[i] > maxDev {
			maxDev = deviations[i]
		}
	}

	amountPct := percentile(list, func(c Candidate) float64 { return c.Amount })
	turnoverPct := percentile(list, func(c Candidate) float64 { return c.Turnover })
	heatPct := percentile(list, func(c Candidate) float64 { return c.Heat })

	for i := range list {
		dev := 0.0
		if maxDev > 0 {
			dev = deviations[i] / maxDev
		}
		raw := dev*weightChangePct +
			amountPct[i]*weightAmount +
			turnoverPct[i]*weightTurnover +
			heatPct[i]*weightBehavior
		// 折算到 0~100，和设计文档里的 hot_score 口径一致，便于排查时直接看
		list[i].Score = math.Round(raw/weightAvailable*10000) / 100
	}
}

// percentile 返回每个位置的分位值（0~1）：并列值取相同分位，池内最小为 0、最大为 1。
func percentile(list []Candidate, value func(Candidate) float64) []float64 {
	n := len(list)
	out := make([]float64, n)
	if n == 1 {
		out[0] = 1
		return out
	}

	idx := make([]int, n)
	for i := range idx {
		idx[i] = i
	}
	sort.SliceStable(idx, func(a, b int) bool {
		return value(list[idx[a]]) < value(list[idx[b]])
	})

	prevValue := 0.0
	prevRank := 0.0
	for rank, i := range idx {
		v := value(list[i])
		if rank > 0 && v == prevValue {
			out[i] = prevRank
			continue
		}
		out[i] = float64(rank) / float64(n-1)
		prevValue, prevRank = v, out[i]
	}
	return out
}

// ---------- Redis 成员编解码 ----------

// encode 按设计文档的 5 段格式：code|name|sector|changePct|volumeRatio。
// 名称与板块由主数据保证不含竖线，解码用 SplitN(…, 5) 就够，不必上 JSON。
func (c Candidate) encode() string {
	return strings.Join([]string{
		c.Code,
		c.Name,
		c.Sector,
		strconv.FormatFloat(c.ChangePct, 'f', 2, 64),
		strconv.FormatFloat(c.VolumeRatio, 'f', 2, 64),
	}, "|")
}

func decodeCandidate(member string, score float64) (Candidate, bool) {
	parts := strings.SplitN(member, "|", 5)
	if len(parts) != 5 || parts[0] == "" || parts[1] == "" {
		return Candidate{}, false
	}
	changePct, err := strconv.ParseFloat(parts[3], 64)
	if err != nil {
		return Candidate{}, false
	}
	volumeRatio, err := strconv.ParseFloat(parts[4], 64)
	if err != nil {
		volumeRatio = 0
	}
	return Candidate{
		Code:        parts[0],
		Name:        parts[1],
		Sector:      parts[2],
		ChangePct:   changePct,
		VolumeRatio: volumeRatio,
		Score:       score,
	}, true
}

// ---------- 站内行为信号 ----------

// bumpAskHeat 记录一次 Quick Ask 点击：网关没有搜索日志与卡片点击埋点，
// 这是「站内行为」这一档权重唯一的来源，也是比成交额更接近
// 「用户此刻想问什么」的信号，比重最高。
func bumpAskHeat(ctx context.Context, code string) {
	if deps.RDB == nil {
		return
	}
	pipe := deps.RDB.Pipeline()
	pipe.ZIncrBy(ctx, askHeatKey, 1, code)
	pipe.Expire(ctx, askHeatKey, askHeatTTL)
	if _, err := pipe.Exec(ctx); err != nil {
		log.Printf("bump ask heat failed: code=%s err=%v", code, err)
	}
}

func loadAskHeat(ctx context.Context) map[string]float64 {
	out := make(map[string]float64)
	if deps.RDB == nil {
		return out
	}
	res, err := deps.RDB.ZRevRangeWithScores(ctx, askHeatKey, 0, maxUniverse-1).Result()
	if err != nil {
		log.Printf("load ask heat failed: %v", err)
		return out
	}
	for _, z := range res {
		if member, ok := z.Member.(string); ok {
			out[member] = z.Score
		}
	}
	return out
}

// loadWatchlistHeat 用全站自选加入数当「关注度」代理：它比站内搜索稀疏，
// 但不需要新增埋点表，且天然排除了从没被任何用户关注过的股票。
func loadWatchlistHeat(ctx context.Context) map[string]float64 {
	out := make(map[string]float64)
	if deps.DB == nil {
		return out
	}
	var rows []struct {
		Code string `gorm:"column:stock_code"`
		Cnt  int64  `gorm:"column:cnt"`
	}
	err := deps.DB.WithContext(ctx).Raw(`
SELECT stock_code, COUNT(*) AS cnt
FROM user_watchlist
WHERE enabled = TRUE
GROUP BY stock_code
ORDER BY cnt DESC
LIMIT ?`, maxUniverse).Scan(&rows).Error
	if err != nil {
		// 自选表缺失也不能让首屏建议整体失败：热度退化为常青股基线
		log.Printf("load watchlist heat failed: %v", err)
		return out
	}
	for _, r := range rows {
		if code, ok := stock.NormalizeCode(r.Code); ok {
			out[code] = float64(r.Cnt)
		}
	}
	return out
}

type metaRow struct {
	Code     string `gorm:"column:code"`
	Name     string `gorm:"column:name"`
	Sector   string `gorm:"column:sector"`
	ListDate string `gorm:"column:list_date"`
	Status   string `gorm:"column:status"`
	IsST     bool   `gorm:"column:is_st"`
}

// loadMeta 读主数据：名称 / 行业 / 上市日期 / ST。表未同步时返回空，
// 候选池退回常青股清单（主数据是 Quick Ask 的增强项，不是前置依赖）。
func loadMeta(ctx context.Context) map[string]metaRow {
	out := make(map[string]metaRow)
	if deps.DB == nil {
		return out
	}
	var rows []metaRow
	// 行业口径不绑 std：生产上东财接口被封，sync-master 落的是证监会口径（std='sina'），
	// 绑 'em' 会让板块全空、同板块约束直接失效。em 优先、其余口径兜底。
	err := deps.DB.WithContext(ctx).Raw(`
SELECT b.code,
       b.name,
       COALESCE(i.l1, '') AS sector,
       COALESCE(to_char(b.list_date, 'YYYY-MM-DD'), '') AS list_date,
       b.status,
       b.is_st
FROM stock_basic b
LEFT JOIN LATERAL (
  SELECT l1 FROM stock_industry
  WHERE code = b.code
  ORDER BY (std = 'em') DESC, updated_at DESC
  LIMIT 1
) i ON TRUE`).Scan(&rows).Error
	if err != nil {
		log.Printf("load stock meta failed: %v", err)
		return out
	}
	for _, r := range rows {
		market := stock.MarketOf(r.Code)
		if market == "" {
			continue
		}
		out[market+r.Code] = r
	}
	return out
}

// freshListing 上市不足 7 个自然日视为新股：没有足够历史量价，
// 模板问「周线形态」「近5日资金」都答不出来。用自然日近似交易日，省掉节假日日历。
func freshListing(listDate string, now time.Time) bool {
	if listDate == "" {
		return false
	}
	d, err := time.Parse("2006-01-02", listDate)
	if err != nil {
		return false
	}
	return now.Sub(d) < 7*24*time.Hour
}

func pickName(current, fallback string) string {
	if strings.TrimSpace(current) != "" {
		return current
	}
	return strings.TrimSpace(fallback)
}

// exRightsMarked 除权除息日交易所给简称加的临时前缀（XD / XR / DR），
// 且往往伴随截断（实测「XD澜起科」）。这种名字拼进问句就是
// 「XD澜起科现在什么价？」，用户按正式简称提问时实体也匹配不到，
// 所以不进候选池：等主数据换回正式简称（下一次同步）自然会回来。
func exRightsMarked(name string) bool {
	up := strings.ToUpper(strings.TrimSpace(name))
	for _, p := range []string{"XD", "XR", "DR"} {
		if strings.HasPrefix(up, p) {
			return true
		}
	}
	return false
}
