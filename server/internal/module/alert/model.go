package alert

import (
	"time"

	"github.com/shopspring/decimal"
)

// 提醒条件枚举，与小程序「新增提醒」弹窗的触发条件一一对应；
// 集中定义，业务代码里不出现魔法字符串
const (
	CondPriceUp   = "price_up"   // 价格突破：最新价 >= 阈值
	CondPriceDown = "price_down" // 价格跌破：最新价 <= 阈值
	CondPctUp     = "pct_up"     // 涨幅提醒：涨跌幅 >= 阈值(%)
	CondPctDown   = "pct_down"   // 跌幅提醒：涨跌幅 <= -阈值(%)
	CondVolume    = "volume"     // 成交量异动：成交量 >= 阈值 × N 日均量
)

// 检查频率
const (
	FreqRealtime = "realtime" // 盘中即时：每次行情刷新都判
	FreqClose    = "close"    // 日线收盘确认：只在收盘后判
)

func ValidCondition(t string) bool {
	switch t {
	case CondPriceUp, CondPriceDown, CondPctUp, CondPctDown, CondVolume:
		return true
	}
	return false
}

func ValidFrequency(f string) bool {
	return f == FreqRealtime || f == FreqClose
}

// ValidVolumeBase 成交量基准（均量天数）只允许 5 / 10 / 20
func ValidVolumeBase(n int) bool {
	return n == 5 || n == 10 || n == 20
}

// AlertRule 对应 alert_rule 表；查询 / 更新 / 删除都必须带 user_id 过滤，防跨用户操作
type AlertRule struct {
	ID        int64  `gorm:"primaryKey;column:id" json:"id"`
	UserID    int64  `gorm:"column:user_id;not null;uniqueIndex:uk_alert_user_stock_cond,priority:1;index:idx_alert_user" json:"user_id"`
	StockCode string `gorm:"column:stock_code;type:varchar(20);not null;uniqueIndex:uk_alert_user_stock_cond,priority:2" json:"stock_code"`
	// 唯一索引第三段：应用层「先查后建」挡不住并发，
	// 同用户同股票同条件只允许一条，交给 DB 约束兜底
	ConditionType   string          `gorm:"column:condition_type;type:varchar(20);not null;uniqueIndex:uk_alert_user_stock_cond,priority:3" json:"condition_type"`
	Threshold       decimal.Decimal `gorm:"column:threshold;type:numeric(18,4);not null" json:"threshold"`
	VolumeBase      int             `gorm:"column:volume_base;default:0" json:"volume_base"` // 仅成交量异动使用，其余为 0
	CheckFrequency  string          `gorm:"column:check_frequency;type:varchar(16);default:'realtime'" json:"check_frequency"`
	StockName       string          `gorm:"column:stock_name;type:varchar(100)" json:"stock_name"`
	Enabled         bool            `gorm:"column:enabled;default:true" json:"enabled"`
	CreatedAt       time.Time       `gorm:"column:created_at;autoCreateTime" json:"created_at"`
	UpdatedAt       time.Time       `gorm:"column:updated_at;autoUpdateTime" json:"updated_at"`
	LastTriggeredAt *time.Time      `gorm:"column:last_triggered_at" json:"last_triggered_at"`
}

func (AlertRule) TableName() string { return "alert_rule" }

// Models 供 db.AutoMigrate 统一收集
var Models = []any{
	&AlertRule{},
}
