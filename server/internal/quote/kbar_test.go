package quote

import (
	"context"
	"os"
	"testing"
)

// 真实响应片段（sh600519 日线 / 前复权）：
// 行格式是 [时间, 开, 收, 高, 低, 量]，不是 OHLC 顺序，解析时最容易把这四个弄反
func TestParseKlineBarsDaily(t *testing.T) {
	body := []byte(`{"code":0,"msg":"","data":{"sh600519":{` +
		`"qfqday":[["2026-10-22","1303.210","1318.490","1322.000","1296.000","42491.000"],` +
		`["2026-10-23","1311.840","1335.000","1343.880","1310.000","57686.000"]],` +
		`"qt":{"sh600519":["1","贵州茅台"]}}}}`)

	bars, err := parseKlineBars(body, "sh600519", "day")
	if err != nil {
		t.Fatalf("parseKlineBars: %v", err)
	}
	if len(bars) != 2 {
		t.Fatalf("want 2 bars, got %d", len(bars))
	}

	b := bars[1]
	if b.D != "2026-10-23" {
		t.Errorf("date = %q", b.D)
	}
	if got := b.O.String(); got != "1311.84" {
		t.Errorf("open = %s, want 1311.84", got)
	}
	if got := b.C.String(); got != "1335" {
		t.Errorf("close = %s, want 1335", got)
	}
	if got := b.H.String(); got != "1343.88" {
		t.Errorf("high = %s, want 1343.88", got)
	}
	if got := b.L.String(); got != "1310" {
		t.Errorf("low = %s, want 1310", got)
	}
	if b.V != 57686 {
		t.Errorf("volume = %d, want 57686", b.V)
	}
}

// 分钟线在量后面还塞了一个 {} 和涨幅列，多余列要忽略；空价格行整根丢掉
func TestParseKlineBarsMinute(t *testing.T) {
	body := []byte(`{"code":0,"data":{"sh600519":{"m5":[` +
		`["202610231435","1331.04","1329.08","1331.04","1328.02","768.00",{},"0.61"],` +
		`["202610231440","","","","","0.00",{},"0.00"]]}}}`)

	bars, err := parseKlineBars(body, "sh600519", "m5")
	if err != nil {
		t.Fatalf("parseKlineBars: %v", err)
	}
	if len(bars) != 1 {
		t.Fatalf("空价格行应被丢掉，want 1 bar, got %d", len(bars))
	}
	if bars[0].V != 768 {
		t.Errorf("volume = %d, want 768", bars[0].V)
	}
}

// 周期白名单与代码校验必须在上游请求之前拦住：period 直接来自客户端
func TestFetchKlineBarsRejectsBadInput(t *testing.T) {
	ctx := context.Background()
	if _, err := FetchKlineBars(ctx, "SH600519", "m30", 10); err == nil {
		t.Error("不支持的周期应报错")
	}
	if _, err := FetchKlineBars(ctx, "ABC123", "day", 10); err == nil {
		t.Error("非法代码应报错")
	}
}

// 联网冒烟（默认跳过）：KLINE_LIVE=1 go test ./internal/quote -run Live -v
// 五个周期各拉一次，确认上游地址与字段下标没变
func TestFetchKlineBarsLive(t *testing.T) {
	if os.Getenv("KLINE_LIVE") == "" {
		t.Skip("set KLINE_LIVE=1 to hit the upstream")
	}
	for _, period := range []string{"m1", "m5", "m15", "day", "week"} {
		bars, err := FetchKlineBars(context.Background(), "SH600519", period, 60)
		if err != nil {
			t.Fatalf("%s: %v", period, err)
		}
		if len(bars) < 2 {
			t.Fatalf("%s: only %d bars", period, len(bars))
		}
		t.Logf("%s: %d bars, last=%s o=%s h=%s l=%s c=%s v=%d",
			period, len(bars), bars[len(bars)-1].D, bars[len(bars)-1].O, bars[len(bars)-1].H,
			bars[len(bars)-1].L, bars[len(bars)-1].C, bars[len(bars)-1].V)
	}
}
