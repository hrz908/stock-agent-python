package watchlist

import (
	"testing"

	"github.com/shopspring/decimal"
)

// newEntry 构造排序 / 统计用的条目：涨跌幅同时写进展示串，模拟真实组装结果。
func newEntry(code, pct string) listEntry {
	p := decimal.RequireFromString(pct)
	return listEntry{
		item: WatchItem{StockCode: code, ChangePct: p.StringFixed(2), Up: p.IsPositive()},
		pct:  p,
	}
}

// 排序必须按条目的真实涨跌幅走：涨跌幅与条目同进同出，换位后不会错配。
func TestSortItemsByPercent(t *testing.T) {
	list := []listEntry{
		newEntry("SH600001", "1.20"),
		newEntry("SH600002", "5.00"),
		newEntry("SH600003", "-2.10"),
		newEntry("SH600004", "3.30"),
	}

	sortItems(list, "up", "")
	wantUp := []string{"SH600002", "SH600004", "SH600001", "SH600003"}
	for i, want := range wantUp {
		if list[i].item.StockCode != want {
			t.Fatalf("涨幅榜第 %d 位 = %s, want %s", i, list[i].item.StockCode, want)
		}
	}

	sortItems(list, "down", "")
	wantDown := []string{"SH600003", "SH600001", "SH600004", "SH600002"}
	for i, want := range wantDown {
		if list[i].item.StockCode != want {
			t.Fatalf("跌幅榜第 %d 位 = %s, want %s", i, list[i].item.StockCode, want)
		}
	}
}

// filter=all 且没显式传 order 时保持库里的顺序（created_at DESC）。
func TestSortItemsKeepsOrderWithoutOrderParam(t *testing.T) {
	list := []listEntry{
		newEntry("SH600001", "1.20"),
		newEntry("SH600002", "5.00"),
	}
	sortItems(list, "all", "")
	if list[0].item.StockCode != "SH600001" || list[1].item.StockCode != "SH600002" {
		t.Fatalf("未传 order 时不该重排：%s, %s", list[0].item.StockCode, list[1].item.StockCode)
	}
}

// 平盘两边都不计，无行情的条目不参与统计。
func TestCountDirections(t *testing.T) {
	list := []listEntry{
		newEntry("SH600001", "1.20"),
		newEntry("SH600002", "-0.80"),
		newEntry("SH600003", "0.00"),
		{item: WatchItem{StockCode: "SH600004", Pending: true}},
	}
	up, down := countDirections(list)
	if up != 1 || down != 1 {
		t.Fatalf("up=%d down=%d, want up=1 down=1", up, down)
	}
}

// 成本为 0（用户没填）时比例记 0，不能除零；全程 decimal 不能出现浮点误差。
func TestCalcProfit(t *testing.T) {
	amount, rate := calcProfit(decimal.RequireFromString("10.5"), decimal.RequireFromString("10"), 100)
	if amount.String() != "50" {
		t.Fatalf("浮盈 = %s, want 50", amount.String())
	}
	if rate.StringFixed(2) != "5.00" {
		t.Fatalf("浮盈比例 = %s, want 5.00", rate.StringFixed(2))
	}

	amount, rate = calcProfit(decimal.RequireFromString("10.5"), decimal.Zero, 100)
	if amount.String() != "1050" || !rate.IsZero() {
		t.Fatalf("无成本时 amount=%s rate=%s, want 1050 / 0", amount.String(), rate.String())
	}
}
