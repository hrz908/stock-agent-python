package quickask

import (
	"strings"
	"testing"
	"time"
)

func TestWeightedSampleNoDuplicate(t *testing.T) {
	pool := []Candidate{
		{Code: "SH600519", Score: 90},
		{Code: "SZ300750", Score: 70},
		{Code: "SZ002594", Score: 50},
		{Code: "SH600036", Score: 10},
	}
	picked := weightedSample(pool, 3)
	if len(picked) != 3 {
		t.Fatalf("picked=%d, want 3", len(picked))
	}
	seen := map[string]bool{}
	for _, c := range picked {
		if seen[c.Code] {
			t.Fatalf("duplicate code %s", c.Code)
		}
		seen[c.Code] = true
	}
}

func TestWeightedSampleZeroScoreFallsBackToUniform(t *testing.T) {
	pool := []Candidate{{Code: "A"}, {Code: "B"}, {Code: "C"}}
	if got := weightedSample(pool, 2); len(got) != 2 {
		t.Fatalf("picked=%d, want 2", len(got))
	}
}

func TestSampleDiverseRespectsSectorCap(t *testing.T) {
	pool := []Candidate{
		{Code: "SH1", Sector: "新能源", Score: 100},
		{Code: "SH2", Sector: "新能源", Score: 99},
		{Code: "SH3", Sector: "新能源", Score: 98},
		{Code: "SH4", Sector: "新能源", Score: 97},
		{Code: "SH5", Sector: "银行", Score: 10},
		{Code: "SH6", Sector: "银行", Score: 9},
	}
	picked := sampleDiverse(pool, 4, 2)
	if len(picked) != 4 {
		t.Fatalf("picked=%d, want 4", len(picked))
	}
	count := map[string]int{}
	for _, c := range picked {
		count[c.Sector]++
	}
	for sector, n := range count {
		if n > 2 {
			t.Fatalf("sector %s picked %d times, want <=2", sector, n)
		}
	}
}

func TestSampleDiverseStillFillsWhenAllInOneSector(t *testing.T) {
	pool := []Candidate{
		{Code: "SH1", Sector: "新能源", Score: 3},
		{Code: "SH2", Sector: "新能源", Score: 2},
		{Code: "SH3", Sector: "新能源", Score: 1},
	}
	// 多样性是软约束：全池同板块时也要给满条数
	if got := sampleDiverse(pool, 3, 2); len(got) != 3 {
		t.Fatalf("picked=%d, want 3", len(got))
	}
}

func TestPickTemplateByState(t *testing.T) {
	cases := []struct {
		name  string
		cand  Candidate
		phase phase
		want  string
	}{
		{"大涨盘中问盘面", Candidate{Name: "宁德时代", ChangePct: 7.2}, phaseTrading, "盘面"},
		{"大跌盘后仍问盘面", Candidate{Name: "宁德时代", ChangePct: -6.1}, phaseAfterClose, "盘面"},
		{"放量问资金", Candidate{Name: "比亚迪", ChangePct: 1.2, VolumeRatio: 2.6}, phaseTrading, "资金"},
		{"盘后横盘问基本面", Candidate{Name: "贵州茅台", ChangePct: 0.4}, phaseAfterClose, "基本面"},
		{"周末横盘问基本面", Candidate{Name: "贵州茅台", ChangePct: 0.4}, phaseWeekend, "基本面"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := pickTemplate(tc.cand, tc.phase).tag; got != tc.want {
				t.Fatalf("tag=%s, want %s", got, tc.want)
			}
		})
	}
}

func TestPickTemplateIntradaySideways(t *testing.T) {
	allowed := map[string]bool{"技术": true, "观察": true, "风险": true}
	for i := 0; i < 40; i++ {
		got := pickTemplate(Candidate{Name: "招商银行", ChangePct: 0.3}, phaseTrading).tag
		if !allowed[got] {
			t.Fatalf("tag=%s, want one of 技术/观察/风险", got)
		}
	}
}

func TestBuildAskFillsAllPlaceholders(t *testing.T) {
	now := time.Date(2026, 10, 26, 10, 0, 0, 0, shanghaiLoc)
	ask := buildAsk(Candidate{Code: "SH600519", Name: "贵州茅台"}, phaseTrading, now, nil)
	if strings.Contains(ask.Display, "{") || strings.Contains(ask.Query, "{") {
		t.Fatalf("placeholder left: %+v", ask)
	}
	if !strings.Contains(ask.Display, "贵州茅台") {
		t.Fatalf("display missing name: %s", ask.Display)
	}
	if !strings.Contains(ask.Query, "600519") {
		t.Fatalf("query missing code: %s", ask.Query)
	}
	if ask.StockCode != "600519" || ask.StockName != "贵州茅台" || ask.Tag == "" {
		t.Fatalf("unexpected ask: %+v", ask)
	}
	if !strings.HasPrefix(ask.ID, "qa_600519_") {
		t.Fatalf("unexpected id: %s", ask.ID)
	}
}

func TestBuildAskRotatesDimensionsInBatch(t *testing.T) {
	now := time.Date(2026, 10, 26, 16, 0, 0, 0, shanghaiLoc)
	codes := []string{"SH600001", "SH600002", "SH600003"}
	used := map[string]int{}
	tags := map[string]bool{}
	for _, code := range codes {
		ask := buildAsk(Candidate{Code: code, Name: "测试股", ChangePct: 0.8}, phaseAfterClose, now, used)
		tags[ask.Tag] = true
	}
	// 盘后横盘有三档维度：三条建议不应全挤在基本面
	if len(tags) < 3 {
		t.Fatalf("tags=%v, want 3 distinct dimensions", tags)
	}
}

func TestParseRankItems(t *testing.T) {
	body := []byte(`[
      {"symbol":"sz300308","code":"300308","name":"中际旭创","changepercent":3.816,"amount":27918480076,"turnoverratio":4.31},
      {"symbol":"sh600519","code":"600519","name":"贵州茅台","changepercent":1.25,"amount":7677216283,"turnoverratio":0.46}
    ]`)
	items, err := parseRankItems(body)
	if err != nil {
		t.Fatalf("parse failed: %v", err)
	}
	if len(items) != 2 {
		t.Fatalf("items=%d, want 2", len(items))
	}
	if items[0].Symbol != "sz300308" || items[0].Name != "中际旭创" || items[0].Amount <= 0 {
		t.Fatalf("unexpected first item: %+v", items[0])
	}
}

func TestParseRankItemsNullPage(t *testing.T) {
	// 翻到空页时新浪返回 null，不能当解析失败
	items, err := parseRankItems([]byte(`null`))
	if err != nil || len(items) != 0 {
		t.Fatalf("items=%v err=%v", items, err)
	}
}

func TestWatchlistUniverseKeepsEvergreenAndDropsColdStocks(t *testing.T) {
	meta := map[string]metaRow{
		"SH600519": {Code: "600519", Name: "贵州茅台", Status: "listed"},
		"BJ920092": {Code: "920092", Name: "汉鑫科技", Status: "listed"},
		"SH600001": {Code: "600001", Name: "ST 样例", Status: "listed", IsST: true},
	}
	heat := map[string]float64{"BJ920092": 3, "SH600001": 2}
	pool := watchlistUniverse(meta, heat)

	codes := map[string]bool{}
	for _, c := range pool {
		codes[c.Code] = true
	}
	if !codes["SH600519"] {
		t.Fatal("evergreen should always be present")
	}
	if !codes["BJ920092"] {
		t.Fatal("watched stock should be present")
	}
	if codes["SH600001"] {
		t.Fatal("ST stock must be filtered out")
	}
}

func TestEncodeDecodeRoundTrip(t *testing.T) {
	in := Candidate{Code: "SZ300750", Name: "宁德时代", Sector: "电力设备", ChangePct: 4.23, VolumeRatio: 2.11}
	out, ok := decodeCandidate(in.encode(), 87.6)
	if !ok {
		t.Fatal("decode failed")
	}
	if out.Code != in.Code || out.Name != in.Name || out.Sector != in.Sector {
		t.Fatalf("identity mismatch: %+v", out)
	}
	if out.ChangePct != in.ChangePct || out.VolumeRatio != in.VolumeRatio {
		t.Fatalf("number mismatch: %+v", out)
	}
	if out.Score != 87.6 {
		t.Fatalf("score=%v, want 87.6", out.Score)
	}
}

func TestDecodeCandidateRejectsBadMember(t *testing.T) {
	if _, ok := decodeCandidate("SH600519|贵州茅台", 1); ok {
		t.Fatal("want decode failure for 2-field member")
	}
	if _, ok := decodeCandidate("SH600519|贵州茅台|食品饮料|abc|1", 1); ok {
		t.Fatal("want decode failure for bad changePct")
	}
}

func TestClampCount(t *testing.T) {
	cases := map[string]int{"": defaultCount, "1": minCount, "4": 4, "9": maxCount, "x": defaultCount}
	for raw, want := range cases {
		if got := clampCount(raw); got != want {
			t.Fatalf("clampCount(%q)=%d, want %d", raw, got, want)
		}
	}
}

func TestSanitizeSession(t *testing.T) {
	if got := sanitizeSession("abc-123_XYZ"); got != "abc-123_XYZ" {
		t.Fatalf("got %q", got)
	}
	for _, raw := range []string{"", "a b", "a|b", "含中文", strings.Repeat("a", maxSessionLen+1)} {
		if got := sanitizeSession(raw); got != "" {
			t.Fatalf("sanitizeSession(%q)=%q, want empty", raw, got)
		}
	}
}

func TestPercentile(t *testing.T) {
	list := []Candidate{{Amount: 10}, {Amount: 30}, {Amount: 20}}
	pct := percentile(list, func(c Candidate) float64 { return c.Amount })
	if pct[0] != 0 || pct[1] != 1 {
		t.Fatalf("min/max percentile wrong: %v", pct)
	}
	if pct[2] <= 0 || pct[2] >= 1 {
		t.Fatalf("middle percentile out of range: %v", pct[2])
	}
}
