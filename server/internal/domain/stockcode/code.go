package stock

import (
	"regexp"
	"strings"
)

// 市场前缀
const (
	MarketSH = "SH"
	MarketSZ = "SZ"
	MarketBJ = "BJ"
)

var (
	codeRe  = regexp.MustCompile(`^(SH|SZ|BJ)\d{6}$`)
	digitRe = regexp.MustCompile(`^\d{6}$`)

	// 交易所前缀表：与小程序前端 EXCHANGES 保持一致，
	// 前端只提交 6 位数字时，后端按同一套规则反推市场
	markets = []struct {
		prefix string
		codes  []string
	}{
		{MarketSH, []string{"600", "601", "603", "605", "688", "689"}},
		{MarketSZ, []string{"000", "001", "002", "003", "300", "301", "302"}},
		{MarketBJ, []string{"43", "83", "87", "88", "920"}},
	}
)

// NormalizeCode 统一成「市场前缀 + 6 位数字」；
// 兼容小程序带空格的展示格式（"SH 600519"），也允许只传 6 位数字
func NormalizeCode(raw string) (string, bool) {
	c := strings.ToUpper(strings.Join(strings.Fields(raw), ""))
	if codeRe.MatchString(c) {
		return c, true
	}
	if !digitRe.MatchString(c) {
		return "", false
	}
	if m := MarketOf(c); m != "" {
		return m + c, true
	}
	return "", false
}

// MarketOf 由 6 位代码推断市场，推断不出返回空串
func MarketOf(digits string) string {
	for _, m := range markets {
		for _, p := range m.codes {
			if strings.HasPrefix(digits, p) {
				return m.prefix
			}
		}
	}
	return ""
}

// DisplayCode 还原成小程序列表里的展示格式："SH 600519"
func DisplayCode(code string) string {
	if len(code) < 3 {
		return code
	}
	return code[:2] + " " + code[2:]
}
