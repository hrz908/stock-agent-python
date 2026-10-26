package watchlist

import (
	"log"
	"sort"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/shopspring/decimal"
	"gorm.io/gorm/clause"

	"stock-agent-server/internal/deps"
	"stock-agent-server/internal/domain/stockcode"
	"stock-agent-server/internal/hook"
	"stock-agent-server/internal/quote"
	"stock-agent-server/internal/resp"
	"stock-agent-server/internal/util"
)

// noQuote 无行情占位，与小程序 pending 态的展示保持一致
const noQuote = "--"

// maxWatchlist 单用户自选股上限（默认档，由 Register 按配置覆盖）。
// 不设上限时一个账号加几千条，refresh 会放大成几百次上游 HTTP 请求
var maxWatchlist = 200

var (
	zero    = decimal.Zero
	hundred = decimal.NewFromInt(100)
)

/* ===================== 请求 / 响应结构 ===================== */

// AddWatchReq 添加自选入参；cost_price / quantity 用指针区分「没传」与「传了 0」
type AddWatchReq struct {
	StockCode string  `json:"stock_code"`
	CostPrice *string `json:"cost_price"`
	Quantity  *int    `json:"quantity"`
}

type AddWatchResp struct {
	ID        int64  `json:"id"`
	StockCode string `json:"stock_code"`
	StockName string `json:"stock_name"`
}

// WatchItem 与小程序自选股列表的一条记录一一对应：
// code / name / price_text / pct_text / up 可直接用于渲染，
// pending 表示刚加入还没有行情（前端展示 --）
type WatchItem struct {
	ID           int64  `json:"id"`
	StockCode    string `json:"stock_code"` // SH600519
	Code         string `json:"code"`       // "SH 600519"，与前端展示格式一致
	Name         string `json:"name"`
	Price        string `json:"price"`          // 原始价格，保留 2 位
	PriceText    string `json:"price_text"`     // 带千分位：1,705.30
	ChangePct    string `json:"change_percent"` // 涨跌幅：1.23
	PctText      string `json:"pct_text"`       // 带符号：+1.23%
	Up           bool   `json:"up"`
	CostPrice    string `json:"cost_price"`
	Quantity     int    `json:"quantity"`
	ProfitAmount string `json:"profit_amount"` // 浮动盈亏金额
	ProfitRate   string `json:"profit_rate"`   // 浮动盈亏比例（%）
	Pending      bool   `json:"pending"`       // 无行情
	Stale        bool   `json:"stale"`         // 用的是旧缓存
	UpdatedAt    string `json:"updated_at"`
}

// WatchListResp 列表响应，含单只盈亏与总盈亏统计
type WatchListResp struct {
	List            []WatchItem `json:"list"`
	TotalProfit     string      `json:"total_profit"`
	TotalProfitRate string      `json:"total_profit_rate"`
	UpCount         int         `json:"up_count"`
	DownCount       int         `json:"down_count"`
}

/* ===================== 查询列表 ===================== */

// GetWatchlist GET /api/v1/watchlist
// 执行顺序：PG 取基础信息 → Redis 批量取实时价 → decimal 算盈亏 → 按涨跌幅排序。
// 降级：Redis 不可用时不报错，按「无行情」处理（pending: true）
func GetWatchlist(c *gin.Context) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")

	var rows []UserWatchlist
	if err := deps.DB.WithContext(ctx).
		Where("user_id = ? AND enabled = ?", userID, true).
		Order("created_at DESC").
		Find(&rows).Error; err != nil {
		log.Printf("query watchlist failed: user=%d err=%v", userID, err)
		resp.Fail(c, resp.CodeInternal, "查询自选股失败")
		return
	}

	codes := make([]string, 0, len(rows))
	for _, r := range rows {
		codes = append(codes, r.StockCode)
	}
	quotes, err := quote.BatchGetQuotes(ctx, codes)
	if err != nil {
		log.Printf("batch get quotes failed, degrade: %v", err)
		quotes = map[string]*quote.StockSnapshot{}
	}

	filter := c.DefaultQuery("filter", "all")
	list, totalProfit, totalCostValue := assembleItems(rows, quotes, nil, filter)
	resp.OK(c, listResponse(list, totalProfit, totalCostValue, filter, c.DefaultQuery("order", "")))
}

/* ===================== 添加自选 ===================== */

// AddWatchlist POST /api/v1/watchlist
// 执行顺序：代码归一化 → 校验 → 拉行情（失败直接 502，不写库）→ Upsert → 落缓存。
// 先拉行情是为了避免「有记录无名称」的脏数据
func AddWatchlist(c *gin.Context) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")

	var req AddWatchReq
	if err := c.ShouldBindJSON(&req); err != nil {
		resp.Fail(c, resp.CodeBadRequest, "请求参数不合法")
		return
	}

	code, ok := stock.NormalizeCode(req.StockCode)
	if !ok {
		resp.Fail(c, resp.CodeBadRequest, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字")
		return
	}

	cost, errMsg := parseCost(req.CostPrice)
	if errMsg != "" {
		resp.Fail(c, resp.CodeBadRequest, errMsg)
		return
	}
	qty, errMsg := parseQuantity(req.Quantity)
	if errMsg != "" {
		resp.Fail(c, resp.CodeBadRequest, errMsg)
		return
	}

	// 数量上限：否则一个账号可加几千条，之后每次 refresh 都放大成几百次上游请求
	var count int64
	if err := deps.DB.WithContext(ctx).Model(&UserWatchlist{}).
		Where("user_id = ? AND enabled = ?", userID, true).
		Count(&count).Error; err != nil {
		log.Printf("count watchlist failed: user=%d err=%v", userID, err)
		resp.Fail(c, resp.CodeInternal, "添加自选股失败")
		return
	}
	if count >= int64(maxWatchlist) {
		resp.Fail(c, resp.CodeBadRequest, "自选股数量已达上限")
		return
	}

	snap, err := quote.FetchOne(ctx, code)
	if err != nil || snap == nil {
		log.Printf("fetch stock quote failed: code=%s err=%v", code, err)
		resp.Fail(c, resp.CodeBadGateway, "行情获取失败，请稍后重试")
		return
	}
	name := snap.Name
	if strings.TrimSpace(name) == "" {
		name = code
	}

	row := UserWatchlist{
		UserID:    userID,
		StockCode: code,
		StockName: name,
		CostPrice: cost,
		Quantity:  qty,
		Enabled:   true,
		// 显式取当前时间：冲突更新要读 row.UpdatedAt，留零值会写成 0001-01-01
		UpdatedAt: time.Now(),
	}
	// 冲突时改哪些列：enabled 必须恢复（「移出后重新添加」靠它），
	// 否则用户看到添加成功、列表里却查不到；
	// 成本价 / 持股数量只「传了才更新」——重复添加时用户往往不再填，无条件覆盖会被冲成 0
	updateCols := []string{"enabled"}
	if req.CostPrice != nil {
		updateCols = append(updateCols, "cost_price")
	}
	if req.Quantity != nil {
		updateCols = append(updateCols, "quantity")
	}
	updateCols = append(updateCols, "updated_at")

	// Upsert：唯一索引冲突时更新上述列，不覆盖股票名称
	if err := deps.DB.WithContext(ctx).Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "user_id"}, {Name: "stock_code"}},
		DoUpdates: clause.AssignmentColumns(updateCols),
	}).Create(&row).Error; err != nil {
		log.Printf("upsert watchlist failed: user=%d code=%s err=%v", userID, code, err)
		resp.Fail(c, resp.CodeInternal, "添加自选股失败")
		return
	}
	// 冲突更新时 GORM 借 Postgres RETURNING 回填主键；没回填就补查一次
	if row.ID == 0 {
		var exist UserWatchlist
		if err := deps.DB.WithContext(ctx).
			Where("user_id = ? AND stock_code = ?", userID, code).
			First(&exist).Error; err == nil {
			row.ID = exist.ID
		}
	}

	// 缓存失败不影响本次添加，只记日志
	if err := quote.SaveQuoteToRedis(ctx, snap); err != nil {
		log.Printf("save quote to redis failed: code=%s err=%v", code, err)
	}

	resp.OK(c, AddWatchResp{ID: row.ID, StockCode: row.StockCode, StockName: row.StockName})
}

/* ===================== 手动刷新 ===================== */

// refreshParams 取刷新的过滤 / 排序参数：query 优先，缺失时读 JSON body，
// 都没有就退回列表接口的默认值
func refreshParams(c *gin.Context) (string, string) {
	filter := c.Query("filter")
	order := c.Query("order")
	if filter == "" || order == "" {
		var body struct {
			Filter string `json:"filter"`
			Order  string `json:"order"`
		}
		// body 为空或不是 JSON 时会报错，按「没传」处理
		if err := c.ShouldBindJSON(&body); err == nil {
			if filter == "" {
				filter = body.Filter
			}
			if order == "" {
				order = body.Order
			}
		}
	}
	if filter == "" {
		filter = "all"
	}
	return filter, order
}

// RefreshWatchlist POST /api/v1/watchlist/refresh
// 执行顺序：查列表 → 并发拉行情（上限 limit）→ pipeline 批量写缓存
//
//	→ 失败降级读旧缓存 → 广播「行情已更新」钩子
//
// 降级：拉取失败先退回 Redis 旧值并标记 stale=true；旧值也没有才 pending=true、价格 0，前端据此显示 --。
// 本接口只更新 Redis，不写 user_watchlist；刷新完成后经 hook 广播，避免模块间互相依赖
func RefreshWatchlist(c *gin.Context) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")

	var rows []UserWatchlist
	if err := deps.DB.WithContext(ctx).
		Where("user_id = ? AND enabled = ?", userID, true).
		Find(&rows).Error; err != nil {
		log.Printf("query watchlist failed: user=%d err=%v", userID, err)
		resp.Fail(c, resp.CodeInternal, "刷新行情失败")
		return
	}

	// 没有自选股也正常返回空数组，走统一响应体
	codes := make([]string, 0, len(rows))
	for _, r := range rows {
		codes = append(codes, r.StockCode)
	}

	fresh, failed := quote.FetchQuotes(ctx, codes)

	if err := quote.BatchSaveQuotes(ctx, util.Values(fresh)); err != nil {
		log.Printf("batch save quotes failed: %v", err)
	}

	// 拉取失败的降级读旧缓存；读不到就保持无行情
	staleMap := map[string]*quote.StockSnapshot{}
	if len(failed) > 0 {
		old, err := quote.BatchGetQuotes(ctx, failed)
		if err != nil {
			log.Printf("fallback read quotes failed: %v", err)
		} else {
			staleMap = old
		}
	}

	// filter/order 同时接受 query 与 JSON body：列表接口是 GET 用 query，
	// 刷新是 POST，小程序把参数放在 body 里，只读 query 会静默丢掉过滤条件
	filter, order := refreshParams(c)
	// 组装与列表接口共用一套：行情优先级 fresh > stale > 无行情
	list, totalProfit, totalCostValue := assembleItems(rows, fresh, staleMap, filter)

	// 行情更新完成，异步广播给关心方（提醒检查等），不拖慢本接口响应
	hook.EmitQuoteUpdatedAsync(userID, ctx)

	// 返回结构与列表接口一致，前端可整段替换列表与统计
	resp.OK(c, listResponse(list, totalProfit, totalCostValue, filter, order))
}

// listResponse 查询与刷新共用：先统计涨跌再排序（统计与顺序无关），返回结构与列表接口一致。
func listResponse(list []listEntry, totalProfit, totalCostValue decimal.Decimal, filter, order string) WatchListResp {
	upCount, downCount := countDirections(list)
	sortItems(list, filter, order)

	items := make([]WatchItem, len(list))
	for i, e := range list {
		items[i] = e.item
	}
	return WatchListResp{
		List:            items,
		TotalProfit:     util.Money(totalProfit),
		TotalProfitRate: sumRate(totalProfit, totalCostValue),
		UpCount:         upCount,
		DownCount:       downCount,
	}
}

// sumRate 总盈亏比例 = 总盈亏 ÷ 总成本；成本为 0 记 0，避免除零
func sumRate(profit, costValue decimal.Decimal) string {
	if !costValue.IsPositive() {
		return "0.00"
	}
	return profit.Div(costValue).Mul(hundred).StringFixed(2)
}

// DeleteWatchlist DELETE /api/v1/watchlist/:stock_code
// 软删除：只把 enabled 置为 false，硬删会连成本价等建仓信息一起丢掉；
// 更新必须带 user_id 过滤，防止改到别人的记录
func DeleteWatchlist(c *gin.Context) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")

	code, ok := stock.NormalizeCode(c.Param("stock_code"))
	if !ok {
		resp.Fail(c, resp.CodeBadRequest, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字")
		return
	}

	res := deps.DB.WithContext(ctx).Model(&UserWatchlist{}).
		Where("user_id = ? AND stock_code = ?", userID, code).
		Update("enabled", false)
	if res.Error != nil {
		log.Printf("delete watchlist failed: user=%d code=%s err=%v", userID, code, res.Error)
		resp.Fail(c, resp.CodeInternal, "移出自选失败")
		return
	}
	if res.RowsAffected == 0 {
		// 查不到统一 404，不区分「不存在」与「不是本人的」
		resp.Fail(c, resp.CodeNotFound, "自选不存在或无权限")
		return
	}

	resp.OK(c, gin.H{"stock_code": code})
}

/* ===================== 组装返回项 ===================== */

// listEntry 组装期的中间结构：涨跌幅与浮盈随条目一起流转。
// 列表项里的这两个字段是展示串，回读再解析会让每条多一次 decimal 解析。
type listEntry struct {
	item   WatchItem
	pct    decimal.Decimal
	profit decimal.Decimal
}

// buildItem 把「数据库行 + 行情快照」组装成列表项，盈亏在后端算好；
// 同时回传未格式化的涨跌幅与浮盈，供排序与统计复用
func buildItem(row UserWatchlist, snap *quote.StockSnapshot, stale bool) listEntry {
	item := WatchItem{
		ID:        row.ID,
		StockCode: row.StockCode,
		Code:      stock.DisplayCode(row.StockCode),
		Name:      row.StockName,
		CostPrice: row.CostPrice.StringFixed(4),
		Quantity:  row.Quantity,
		Stale:     stale,
		UpdatedAt: row.UpdatedAt.Format(time.RFC3339),
	}
	if snap == nil || snap.Price.IsZero() {
		item.Pending = true
		item.Price = "0"
		item.PriceText = noQuote
		item.ChangePct = "0"
		item.PctText = noQuote
		item.ProfitAmount = "0.00"
		item.ProfitRate = "0.00"
		return listEntry{item: item}
	}

	amount, rate := calcProfit(snap.Price, row.CostPrice, row.Quantity)
	item.Price = util.Money(snap.Price)
	item.PriceText = util.Thousands(snap.Price)
	item.ChangePct = snap.ChangePercent.StringFixed(2)
	item.PctText = util.WithSign(snap.ChangePercent) + "%"
	item.Up = snap.ChangePercent.IsPositive()
	item.ProfitAmount = util.Money(amount)
	item.ProfitRate = rate.StringFixed(2)
	return listEntry{item: item, pct: snap.ChangePercent, profit: amount}
}

// assembleItems 列表 / 刷新共用的组装逻辑：涨跌幅榜过滤掉无行情的（与前端 filterList 口径一致），
// 并顺手累计总盈亏；fresh 优先于 stale（stale 表示用的是旧缓存降级数据）
func assembleItems(rows []UserWatchlist, fresh, stale map[string]*quote.StockSnapshot, filter string) ([]listEntry, decimal.Decimal, decimal.Decimal) {
	skipPending := filter == "up" || filter == "down"

	list := make([]listEntry, 0, len(rows))
	totalProfit := zero
	totalCostValue := zero

	for _, r := range rows {
		snap, isStale := fresh[r.StockCode], false
		if snap == nil {
			snap, isStale = stale[r.StockCode], true
		}

		entry := buildItem(r, snap, isStale)
		if skipPending && entry.item.Pending {
			continue
		}
		list = append(list, entry)
		if entry.item.Pending {
			continue // 无行情不计入盈亏统计
		}
		totalProfit = totalProfit.Add(entry.profit)
		totalCostValue = totalCostValue.Add(r.CostPrice.Mul(decimal.NewFromInt(int64(r.Quantity))))
	}
	return list, totalProfit, totalCostValue
}

// calcProfit 浮动盈亏 =（最新价 - 成本价）× 数量，全程 decimal 避免浮点误差。
// 成本为 0（用户没填）时比例记 0，避免除零
func calcProfit(price, cost decimal.Decimal, quantity int) (amount, rate decimal.Decimal) {
	qty := decimal.NewFromInt(int64(quantity))
	amount = price.Sub(cost).Mul(qty)
	costValue := cost.Mul(qty)
	if costValue.IsPositive() {
		rate = amount.Div(costValue).Mul(hundred)
	}
	return amount, rate
}

// sortItems 按涨跌幅排序：filter=up 降序、down 升序；
// all 时只有显式传 order 才排序，否则保持 SQL 的 created_at DESC 顺序
func sortItems(list []listEntry, filter, order string) {
	var desc bool
	switch filter {
	case "up":
		desc = true
	case "down":
		desc = false
	case "all":
		if order != "asc" && order != "desc" {
			return
		}
		desc = order == "desc"
	default:
		return
	}

	// 用 SliceStable：同涨跌幅的条目保持 SQL 顺序，列表不会在两次刷新间随机换位
	sort.SliceStable(list, func(i, j int) bool {
		if desc {
			return list[i].pct.GreaterThan(list[j].pct)
		}
		return list[i].pct.LessThan(list[j].pct)
	})
}

// countDirections 统计涨跌家数：平盘两边都不计，与前端涨跌榜口径一致
func countDirections(list []listEntry) (up, down int) {
	for _, e := range list {
		if e.item.Pending {
			continue
		}
		if e.item.Up {
			up++
		} else if e.pct.IsNegative() {
			down++
		}
	}
	return up, down
}

// 业务上限。DB 列是 numeric(18,4) / int，超范围要等写库才暴露且返回 500；
// 在入口按 400 挡掉，前端拿到的约束也更明确
var (
	maxCostPrice = decimal.NewFromInt(100000000) // 1 亿
	maxQuantity  = int64(100000000)              // 1 亿股
)

// parseCost 解析成本价：未传返回零值；传了必须是数字、非负且不超上限
func parseCost(raw *string) (decimal.Decimal, string) {
	if raw == nil {
		return zero, ""
	}
	v, err := decimal.NewFromString(strings.TrimSpace(*raw))
	if err != nil {
		return zero, "成本价必须是数字"
	}
	if v.IsNegative() {
		return zero, "成本价不能为负"
	}
	if v.GreaterThan(maxCostPrice) {
		return zero, "成本价超出允许范围"
	}
	return v, ""
}

// parseQuantity 解析持股数量：未传为 0；传了不可为负且不超上限
func parseQuantity(raw *int) (int, string) {
	if raw == nil {
		return 0, ""
	}
	if *raw < 0 {
		return 0, "持股数量不能为负"
	}
	if int64(*raw) > maxQuantity {
		return 0, "持股数量超出允许范围"
	}
	return *raw, ""
}
