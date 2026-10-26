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

// allTemplateGroups 全量模板组：新增维度时记得挂进来，下面的约束测试才会覆盖到。
func allTemplateGroups() map[string][]tpl {
	return map[string][]tpl{
		"snapshot":    snapshotTemplates,
		"closed":      closedSnapshotTemplates,
		"capital":     capitalTemplates,
		"fundamental": fundamentalTemplates,
		"technical":   technicalTemplates,
		"risk":        riskTemplates,
		"watch":       watchTemplates,
	}
}

// TestTemplatesStickToAnswerableDimensions 模板只能问「取数工具答得出」的东西。
// 北向持仓 / 筹码分布 / 周线形态都没有数据源，问出去只会答「取不到」；
// 止损、仓位属于投资建议红线；「用一句话总结，再补一句」是提示词口吻，会被原样显示在气泡里。
func TestTemplatesStickToAnswerableDimensions(t *testing.T) {
	banned := []string{
		"北向", "筹码", "周线", "分钟线", "止损", "仓位", "多空",
		"用一句话", "再补一句", "按重要性排序", "可执行", "亮点和雷点",
	}
	for group, list := range allTemplateGroups() {
		for _, tp := range list {
			for _, w := range banned {
				if strings.Contains(tp.display, w) || strings.Contains(tp.ask, w) {
					t.Fatalf("%s 组模板含可疑表述 %q: %+v", group, w, tp)
				}
			}
		}
	}
}

// TestTemplatesNeedOnlyStockName 每条模板只靠 name/code 就能拼出完整问句。
func TestTemplatesNeedOnlyStockName(t *testing.T) {
	replacer := strings.NewReplacer("{name}", "宇树科技", "{code}", "688836")
	for group, list := range allTemplateGroups() {
		for _, tp := range list {
			if !strings.Contains(tp.display, "{name}") {
				t.Fatalf("%s 组 display 缺 {name}: %+v", group, tp)
			}
			if !strings.Contains(tp.ask, "{code}") {
				t.Fatalf("%s 组 ask 缺 {code}（取数要代码落地）: %+v", group, tp)
			}
			out := replacer.Replace(tp.display + " | " + tp.ask)
			if strings.ContainsAny(out, "{}") {
				t.Fatalf("%s 组仍有未替换占位符: %s", group, out)
			}
			if !strings.Contains(out, "宇树科技") || !strings.Contains(out, "688836") {
				t.Fatalf("%s 组拼装后缺名称或代码: %s", group, out)
			}
		}
	}
}

// TestBatchHasNoRepeatedSentence 同一批四条建议不出现两句一模一样的话。
// 覆盖三种取模板路径：盘中异动（只准问盘面）、盘中横盘（技术/观察/风险）、盘后（基本面/风险/观察）。
func TestBatchHasNoRepeatedSentence(t *testing.T) {
	now := time.Date(2026, 10, 26, 16, 0, 0, 0, shanghaiLoc)
	cases := []struct {
		name string
		p    phase
		chg  float64
		vr   float64
	}{
		{"盘中异动", phaseTrading, 6.5, 0},
		{"盘中横盘", phaseTrading, 0.5, 0},
		{"盘中放量", phaseTrading, 1.2, 3},
		{"盘后横盘", phaseAfterClose, 0.5, 0},
	}
	for _, tc := range cases {
		for trial := 0; trial < 50; trial++ {
			used := map[string]int{}
			seen := map[string]bool{}
			for i := 0; i < 4; i++ {
				ask := buildAsk(
					Candidate{
						Code:        "SH60000" + string(rune('1'+i)),
						Name:        "测试股",
						ChangePct:   tc.chg,
						VolumeRatio: tc.vr,
					},
					tc.p,
					now,
					used,
				)
				if seen[ask.Query] {
					t.Fatalf("%s 批内出现重复问句: %s", tc.name, ask.Query)
				}
				seen[ask.Query] = true
			}
		}
	}
}

// TestExRightsNameFiltered 除权除息临时简称（XD/XR/DR，且常被截断）不进候选池。
func TestExRightsNameFiltered(t *testing.T) {
	for _, name := range []string{"XD澜起科", "XR某某股", "dr测试"} {
		if !exRightsMarked(name) {
			t.Fatalf("临时简称未识别: %s", name)
		}
	}
	for _, name := range []string{"澜起科技", "贵州茅台", ""} {
		if exRightsMarked(name) {
			t.Fatalf("正常简称被误判: %s", name)
		}
	}

	meta := map[string]metaRow{
		"SH688008": {Code: "688008", Name: "XD澜起科", Status: "listed"},
		"SH600519": {Code: "600519", Name: "贵州茅台", Status: "listed"},
	}
	pool := watchlistUniverse(meta, map[string]float64{"SH688008": 5, "SH600519": 2})
	for _, c := range pool {
		if c.Code == "SH688008" {
			t.Fatal("除权临时简称不该进候选池")
		}
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
