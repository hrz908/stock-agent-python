package alert

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/shopspring/decimal"
	"gorm.io/gorm"

	"stock-agent-server/internal/deps"
	"stock-agent-server/internal/domain/stockcode"
	"stock-agent-server/internal/quote"
	"stock-agent-server/internal/resp"
	"stock-agent-server/internal/util"
	"stock-agent-server/internal/wecom"
)

// 提醒防抖锁 TTL（秒）
const alertLockTTL = 60

// stockNameMax 股票名称长度上限，对应列类型 varchar(100)
const stockNameMax = 100

// closeHour 收盘确认型的生效时刻：A 股 15:00 收盘，收盘前不判 close 型规则。
// 取服务器本地时间，部署时把时区设为 Asia/Shanghai 即可
const closeHour = 15

/* ===================== 请求 / 响应结构 ===================== */

type CreateAlertReq struct {
	StockCode      string `json:"stock_code"`
	StockName      string `json:"stock_name"`
	ConditionType  string `json:"condition_type"`
	Threshold      string `json:"threshold"`
	VolumeBase     int    `json:"volume_base"`
	CheckFrequency string `json:"check_frequency"`
}

// UpdateAlertReq 局部更新：指针为空表示「不改这个字段」
type UpdateAlertReq struct {
	ConditionType  *string `json:"condition_type"`
	Threshold      *string `json:"threshold"`
	VolumeBase     *int    `json:"volume_base"`
	CheckFrequency *string `json:"check_frequency"`
	Enabled        *bool   `json:"enabled"`
}

// AlertItem 提醒规则；current_price / change_percent 来自 Redis，读不到即为 nil
type AlertItem struct {
	ID              int64   `json:"id"`
	StockCode       string  `json:"stock_code"`
	StockName       string  `json:"stock_name"`
	ConditionType   string  `json:"condition_type"`
	Threshold       string  `json:"threshold"`
	VolumeBase      int     `json:"volume_base"`
	CheckFrequency  string  `json:"check_frequency"`
	Enabled         bool    `json:"enabled"`
	CurrentPrice    *string `json:"current_price"`
	ChangePercent   *string `json:"change_percent"`
	LastTriggeredAt *string `json:"last_triggered_at"`
	CreatedAt       string  `json:"created_at"`
}

/* ===================== 校验约定 =====================
代码 / 条件类型 / 阈值 / 基准周期 / 频率非法 → 400；规则不属于本人 → 404（不区分「不存在」）；
同用户同股票同条件重复 → 409。
=========================================================== */

/* ===================== 新建 ===================== */

// CreateAlert POST /api/v1/alerts
func CreateAlert(c *gin.Context) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")

	var req CreateAlertReq
	if err := c.ShouldBindJSON(&req); err != nil {
		resp.Fail(c, resp.CodeBadRequest, "请求参数不合法")
		return
	}

	code, ok := stock.NormalizeCode(req.StockCode)
	if !ok {
		resp.Fail(c, resp.CodeBadRequest, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字")
		return
	}
	condType := normalizeCondition(req.ConditionType)
	if !ValidCondition(condType) {
		resp.Fail(c, resp.CodeBadRequest, "条件类型不合法")
		return
	}
	threshold, errMsg := parseThreshold(condType, req.Threshold)
	if errMsg != "" {
		resp.Fail(c, resp.CodeBadRequest, errMsg)
		return
	}
	volumeBase, errMsg := normalizeVolumeBase(condType, req.VolumeBase)
	if errMsg != "" {
		resp.Fail(c, resp.CodeBadRequest, errMsg)
		return
	}
	freq := strings.TrimSpace(req.CheckFrequency)
	if freq == "" {
		freq = FreqRealtime
	}
	if !ValidFrequency(freq) {
		resp.Fail(c, resp.CodeBadRequest, "检查频率不合法")
		return
	}
	// 名称列是 varchar(100)，超长时错误来自 DB 且返回 500，在入口按 400 挡掉
	stockName := strings.TrimSpace(req.StockName)
	if utf8.RuneCountInString(stockName) > stockNameMax {
		resp.Fail(c, resp.CodeBadRequest, "股票名称过长")
		return
	}

	// 同用户 + 同股票 + 同条件只允许一条启用规则。这里只是给用户的友好提示；
	// 并发下两个请求可能同时通过，真正的保证是唯一索引（冲突时下面的 Create 返回 409）
	var exist AlertRule
	err := deps.DB.WithContext(ctx).
		Where("user_id = ? AND stock_code = ? AND condition_type = ? AND enabled = ?",
			userID, code, condType, true).
		First(&exist).Error
	if err == nil {
		resp.Fail(c, resp.CodeConflict, "该股票的该条件提醒已存在")
		return
	}
	if !errors.Is(err, gorm.ErrRecordNotFound) {
		log.Printf("query alert rule failed: user=%d err=%v", userID, err)
		resp.Fail(c, resp.CodeInternal, "创建提醒失败")
		return
	}

	rule := AlertRule{
		UserID:         userID,
		StockCode:      code,
		StockName:      stockName,
		ConditionType:  condType,
		Threshold:      threshold,
		VolumeBase:     volumeBase,
		CheckFrequency: freq,
		Enabled:        true,
	}
	if err := deps.DB.WithContext(ctx).Create(&rule).Error; err != nil {
		if errors.Is(err, gorm.ErrDuplicatedKey) {
			resp.Fail(c, resp.CodeConflict, "该股票的该条件提醒已存在")
			return
		}
		log.Printf("create alert rule failed: user=%d code=%s err=%v", userID, code, err)
		resp.Fail(c, resp.CodeInternal, "创建提醒失败")
		return
	}

	resp.OK(c, toAlertItem(rule, nil))
}

/* ===================== 列表 ===================== */

// ListAlerts GET /api/v1/alerts（可按 stock_code 过滤）
func ListAlerts(c *gin.Context) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")

	q := deps.DB.WithContext(ctx).Where("user_id = ?", userID)
	if raw := strings.TrimSpace(c.Query("stock_code")); raw != "" {
		code, ok := stock.NormalizeCode(raw)
		if !ok {
			resp.Fail(c, resp.CodeBadRequest, "股票代码格式不正确，应为 SH/SZ/BJ + 6 位数字")
			return
		}
		q = q.Where("stock_code = ?", code)
	}

	var rules []AlertRule
	if err := q.Order("created_at DESC").Find(&rules).Error; err != nil {
		log.Printf("list alert rules failed: user=%d err=%v", userID, err)
		resp.Fail(c, resp.CodeInternal, "查询提醒失败")
		return
	}

	// 批量读当前价，前端可直接展示「距离目标还差多少」
	quotes, err := quote.BatchGetQuotes(ctx, uniqueCodes(rules))
	if err != nil {
		log.Printf("batch get quotes for alerts failed: %v", err)
		quotes = map[string]*quote.StockSnapshot{}
	}

	items := make([]AlertItem, 0, len(rules))
	for _, r := range rules {
		items = append(items, toAlertItem(r, quotes[r.StockCode]))
	}
	resp.OK(c, items)
}

/* ===================== 修改 ===================== */

// UpdateAlert PUT /api/v1/alerts/:id
func UpdateAlert(c *gin.Context) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")

	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil || id <= 0 {
		resp.Fail(c, resp.CodeBadRequest, "规则 id 不合法")
		return
	}

	var req UpdateAlertReq
	if err := c.ShouldBindJSON(&req); err != nil {
		resp.Fail(c, resp.CodeBadRequest, "请求参数不合法")
		return
	}

	// 用 id + user_id 定位，查不到统一 404；刻意不区分「不存在」与「不是本人的」，
	// 避免被枚举他人规则 id
	var rule AlertRule
	if err := deps.DB.WithContext(ctx).
		Where("id = ? AND user_id = ?", id, userID).
		First(&rule).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			resp.Fail(c, resp.CodeNotFound, "规则不存在或无权限")
			return
		}
		log.Printf("query alert rule failed: id=%d err=%v", id, err)
		resp.Fail(c, resp.CodeInternal, "修改提醒失败")
		return
	}

	// 条件类型最终值：请求带了用新的，否则沿用库里的；阈值 / 基准周期校验要跟着最终值走
	condType := rule.ConditionType
	if req.ConditionType != nil {
		condType = normalizeCondition(*req.ConditionType)
		if !ValidCondition(condType) {
			resp.Fail(c, resp.CodeBadRequest, "条件类型不合法")
			return
		}
	}

	updates := map[string]any{}
	// updated 与 updates 同步维护：写库后不必再整行回查，直接用它组装响应
	updated := rule
	if req.ConditionType != nil {
		updates["condition_type"] = condType
		updated.ConditionType = condType
	}
	if req.Threshold != nil {
		v, msg := parseThreshold(condType, *req.Threshold)
		if msg != "" {
			resp.Fail(c, resp.CodeBadRequest, msg)
			return
		}
		updates["threshold"] = v
		updated.Threshold = v
	}
	if req.VolumeBase != nil {
		n, msg := normalizeVolumeBase(condType, *req.VolumeBase)
		if msg != "" {
			resp.Fail(c, resp.CodeBadRequest, msg)
			return
		}
		updates["volume_base"] = n
		updated.VolumeBase = n
	} else if condType != CondVolume {
		// 从成交量切成其它条件时基准周期必须归 0，
		// 否则库里残留 20/10/5，与创建时的归一化不一致
		updates["volume_base"] = 0
		updated.VolumeBase = 0
	}
	if req.CheckFrequency != nil {
		if !ValidFrequency(*req.CheckFrequency) {
			resp.Fail(c, resp.CodeBadRequest, "检查频率不合法")
			return
		}
		updates["check_frequency"] = *req.CheckFrequency
		updated.CheckFrequency = *req.CheckFrequency
	}
	// 判定条件变了就清掉上次触发时间，让它能重新提醒
	if len(updates) > 0 {
		updates["last_triggered_at"] = nil
		updated.LastTriggeredAt = nil
	}
	if req.Enabled != nil {
		updates["enabled"] = *req.Enabled
		updated.Enabled = *req.Enabled
	}

	if len(updates) == 0 {
		resp.OK(c, toAlertItem(rule, nil))
		return
	}
	// 更新同样带 user_id 过滤，双重保险
	if err := deps.DB.WithContext(ctx).Model(&AlertRule{}).
		Where("id = ? AND user_id = ?", id, userID).
		Updates(updates).Error; err != nil {
		log.Printf("update alert rule failed: id=%d err=%v", id, err)
		resp.Fail(c, resp.CodeInternal, "修改提醒失败")
		return
	}

	// 停用即解锁：清掉防抖锁，避免残留锁挡住后续推送
	if req.Enabled != nil && !*req.Enabled {
		delAlertLock(ctx, userID, rule)
	}

	snap, _ := quote.GetQuoteFromRedis(ctx, updated.StockCode)
	resp.OK(c, toAlertItem(updated, snap))
}

/* ===================== 删除 ===================== */

// DeleteAlert DELETE /api/v1/alerts/:id
// 硬删：表无 deleted_at，规则是用户私有资源，不需要回收站；若后续要审计再改软删
func DeleteAlert(c *gin.Context) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")

	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil || id <= 0 {
		resp.Fail(c, resp.CodeBadRequest, "规则 id 不合法")
		return
	}

	var rule AlertRule
	if err := deps.DB.WithContext(ctx).
		Where("id = ? AND user_id = ?", id, userID).
		First(&rule).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			resp.Fail(c, resp.CodeNotFound, "规则不存在或无权限")
			return
		}
		log.Printf("query alert rule failed: id=%d err=%v", id, err)
		resp.Fail(c, resp.CodeInternal, "删除提醒失败")
		return
	}

	if err := deps.DB.WithContext(ctx).
		Where("id = ? AND user_id = ?", id, userID).
		Delete(&AlertRule{}).Error; err != nil {
		log.Printf("delete alert rule failed: id=%d err=%v", id, err)
		resp.Fail(c, resp.CodeInternal, "删除提醒失败")
		return
	}

	// 删除后清防抖锁，否则残留锁会挡住同一股票后续新建的规则
	delAlertLock(ctx, userID, rule)

	resp.OK(c, gin.H{"id": id})
}

/* ===================== 刷新后的提醒检查 ===================== */

// checkAlerts 检查该用户的提醒规则，命中就推送企业微信（应用消息 + 群机器人两条通道）。
// 防重复：SetNX alert:lock:{user_id}:{stock_code}:{condition_type}，TTL 60 秒，
// 写不进去说明 60 秒内已经推过
func checkAlerts(userID int64, ctx context.Context) {
	var rules []AlertRule
	if err := deps.DB.WithContext(ctx).
		Where("user_id = ? AND enabled = ?", userID, true).
		Find(&rules).Error; err != nil {
		log.Printf("query alert rules failed: user=%d err=%v", userID, err)
		return
	}
	if len(rules) == 0 {
		return
	}

	quotes, err := quote.BatchGetQuotes(ctx, uniqueCodes(rules))
	if err != nil {
		log.Printf("batch get quotes for alerts failed: %v", err)
		return
	}

	now := time.Now()
	for _, r := range rules {
		snap := quotes[r.StockCode]
		if snap == nil {
			continue // 没行情不判
		}
		// 收盘确认型只在收盘后判定，盘中即时型每次刷新都判
		if r.CheckFrequency == FreqClose && now.Hour() < closeHour {
			continue
		}
		if !isHit(r, snap) {
			continue
		}

		if deps.RDB != nil {
			ok, err := deps.RDB.SetNX(ctx, alertLockKey(userID, r), now.Unix(),
				time.Duration(alertLockTTL)*time.Second).Result()
			if err != nil {
				log.Printf("set alert lock failed: code=%s err=%v", r.StockCode, err)
				continue
			}
			if !ok {
				continue // 60 秒内已推过
			}
		}

		// 企业微信两条通道：应用消息（@all，需可信 IP）与群机器人（Webhook）。
		// 异步发送避免同步 HTTP 阻塞刷新循环，失败只记日志
		if wecom.Enabled() || wecom.WebhookEnabled() {
			content := buildWecomText(snap, r, now)
			if wecom.Enabled() {
				go safeWecomPush("app", r.StockCode, func() error { return wecom.SendTextToAll(content) })
			}
			if wecom.WebhookEnabled() {
				go safeWecomPush("webhook", r.StockCode, func() error { return wecom.SendTextToWebhook(content) })
			}
		}

		if err := deps.DB.WithContext(ctx).Model(&AlertRule{}).
			Where("id = ? AND user_id = ?", r.ID, userID).
			Update("last_triggered_at", now).Error; err != nil {
			log.Printf("update last_triggered_at failed: id=%d err=%v", r.ID, err)
		}
	}
}

// isHit 判断单条规则是否命中行情快照
func isHit(r AlertRule, snap *quote.StockSnapshot) bool {
	// 停牌 / 自建接口返 0 时价格为 0，0 <= 正阈值会让「跌破」类规则恒真，用户会收到假提醒；没行情就不判
	if snap == nil || snap.Price.IsZero() {
		return false
	}
	switch r.ConditionType {
	case CondPriceUp:
		return snap.Price.GreaterThanOrEqual(r.Threshold)
	case CondPriceDown:
		return snap.Price.LessThanOrEqual(r.Threshold)
	case CondPctUp:
		return snap.ChangePercent.GreaterThanOrEqual(r.Threshold)
	case CondPctDown:
		// 前端填的是「跌多少」的正数，比较时取负
		return snap.ChangePercent.LessThanOrEqual(r.Threshold.Neg())
	case CondVolume:
		// 快照里没有成交量字段，成交量异动只存规则、暂不自动判触发；
		// 等 quote 补上成交量与 N 日均量后在这里补比较逻辑即可
		return false
	default:
		return false
	}
}

/* ===================== 内部工具 ===================== */

// toAlertItem 组装返回项，附 Redis 里的当前价与涨跌幅；
// 缓存未命中时这两个字段为 nil，前端据此显示「暂无报价」
func toAlertItem(r AlertRule, snap *quote.StockSnapshot) AlertItem {
	item := AlertItem{
		ID:             r.ID,
		StockCode:      r.StockCode,
		StockName:      r.StockName,
		ConditionType:  r.ConditionType,
		Threshold:      r.Threshold.StringFixed(2),
		VolumeBase:     r.VolumeBase,
		CheckFrequency: r.CheckFrequency,
		Enabled:        r.Enabled,
		CreatedAt:      r.CreatedAt.Format(time.RFC3339),
	}
	if r.LastTriggeredAt != nil {
		t := r.LastTriggeredAt.Format(time.RFC3339)
		item.LastTriggeredAt = &t
	}
	if snap != nil {
		p := util.Money(snap.Price)
		item.CurrentPrice = &p
		cp := snap.ChangePercent.StringFixed(2)
		item.ChangePercent = &cp
	}
	return item
}

// alertLockKey 防抖锁键：alert:lock:{user_id}:{stock_code}:{condition_type}，
// 带上条件类型，同一股票的多条不同条件规则互不干扰
func alertLockKey(userID int64, r AlertRule) string {
	return fmt.Sprintf("alert:lock:%d:%s:%s", userID, r.StockCode, r.ConditionType)
}

// delAlertLock 清掉某条规则的防抖锁，失败只记日志
func delAlertLock(ctx context.Context, userID int64, r AlertRule) {
	if deps.RDB == nil {
		return
	}
	if err := deps.RDB.Del(ctx, alertLockKey(userID, r)).Err(); err != nil {
		log.Printf("del alert lock failed: code=%s err=%v", r.StockCode, err)
	}
}

// safeWecomPush 企业微信旁路推送：独立 goroutine 不经过 gin 的 Recovery，
// 必须自带 panic 兜底，否则一次 panic 会击穿整个进程
func safeWecomPush(channel, stockCode string, send func() error) {
	defer func() {
		if p := recover(); p != nil {
			log.Printf("wecom %s push panic: code=%s err=%v", channel, stockCode, p)
		}
	}()
	if err := send(); err != nil {
		log.Printf("wecom %s push failed: code=%s err=%v", channel, stockCode, err)
	}
}

// uniqueCodes 规则里的股票代码去重（供批量读行情用）
func uniqueCodes(rules []AlertRule) []string {
	codes := make([]string, 0, len(rules))
	for _, r := range rules {
		codes = append(codes, r.StockCode)
	}
	return util.Unique(codes)
}

// parseThreshold 解析阈值：必须是数字，且落在对应条件的取值范围内
func parseThreshold(condType, raw string) (decimal.Decimal, string) {
	v, err := decimal.NewFromString(strings.TrimSpace(raw))
	if err != nil {
		return decimal.Zero, "阈值必须是数字"
	}
	switch condType {
	case CondPriceUp, CondPriceDown:
		if !v.IsPositive() {
			return decimal.Zero, "价格阈值必须大于 0"
		}
	case CondPctUp, CondPctDown:
		if !v.IsPositive() || v.GreaterThan(decimal.NewFromInt(100)) {
			return decimal.Zero, "涨跌幅阈值需在 0 ~ 100 之间"
		}
	case CondVolume:
		if v.LessThan(decimal.NewFromFloat(0.1)) || v.GreaterThan(decimal.NewFromInt(100)) {
			return decimal.Zero, "成交量倍数需在 0.1 ~ 100 之间"
		}
	}
	return v, ""
}

// normalizeVolumeBase 只有成交量异动需要基准周期，且必须在 5 / 10 / 20 内；
// 其他条件一律归 0，避免库里留下无意义取值。
// 成交量条件下传 0 表示「用默认 20 日均量」，不是报错
func normalizeVolumeBase(condType string, n int) (int, string) {
	if condType != CondVolume {
		return 0, ""
	}
	if n == 0 {
		return 20, ""
	}
	if !ValidVolumeBase(n) {
		return 0, "基准周期只能是 5 / 10 / 20 日均量"
	}
	return n, ""
}

// normalizeCondition 条件类型统一小写去空格（与 CheckFrequency 一致），
// 否则 " price_up" 会被判成非法
func normalizeCondition(t string) string {
	return strings.ToLower(strings.TrimSpace(t))
}

// buildWecomText 组装企业微信文本消息；text 类型不渲染 markdown，故用纯文本
func buildWecomText(snap *quote.StockSnapshot, r AlertRule, t time.Time) string {
	name := r.StockName
	if name == "" {
		name = snap.Name
	}
	var b strings.Builder
	b.WriteString("📊 股票提醒\n")
	fmt.Fprintf(&b, "%s（%s）%s\n", name, r.StockCode, conditionLabel(r.ConditionType))
	fmt.Fprintf(&b, "当前价：%s\n", util.Money(snap.Price))
	fmt.Fprintf(&b, "涨跌幅：%s%%\n", snap.ChangePercent.StringFixed(2))
	fmt.Fprintf(&b, "触发阈值：%s\n", r.Threshold.StringFixed(2))
	b.WriteString("时间：" + t.Format("2006-01-02 15:04:05"))
	return b.String()
}

// conditionLabel 条件类型的中文描述，用于企业微信文案
func conditionLabel(condType string) string {
	switch condType {
	case CondPriceUp:
		return "价格突破"
	case CondPriceDown:
		return "价格跌破"
	case CondPctUp:
		return "涨幅提醒"
	case CondPctDown:
		return "跌幅提醒"
	case CondVolume:
		return "成交量异动"
	default:
		return "提醒触发"
	}
}
