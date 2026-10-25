package quote

import (
	"context"
	"io"
	"log"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"

	"stock-agent-server/internal/httpclient"
)

// smartBoxURL 腾讯 smartbox 搜索接口
const smartBoxURL = "https://smartbox.gtimg.cn/s3/"

// maxSearchBodyBytes 搜索响应读取上限，与行情接口同档
const maxSearchBodyBytes = 1 << 20

// unicodeEscape 匹配腾讯返回的 \uXXXX 转义序列
var unicodeEscape = regexp.MustCompile(`\\u[0-9a-fA-F]{4}`)

// varAssignRe 匹配 `v_hint="..."` 这类赋值片段；
// 用正则而非 Index(`="`) + LastIndex(`"`)，后者在响应出现多余引号时会截错范围。
var varAssignRe = regexp.MustCompile(`v_[a-z0-9]+="([^"]*)"`)

// searchHTTP 与行情接口分开建 client（超时不同，见 quote.Init）：
// 搜索高频，复用连接避免重复握手；超时比行情宽 2s，宁可多等也不要假超时返回空列表。
var searchHTTP = httpclient.New(5 * time.Second)

// SearchResult 单条搜索结果：只给前端真正需要的字段，
// 带上 market 让前端不必再用代码前缀反推交易所。
type SearchResult struct {
	Code   string `json:"code"`   // 6 位代码
	Market string `json:"market"` // SH / SZ / BJ
	Name   string `json:"name"`   // 中文名
}

// Search 模糊搜索股票，代理腾讯 smartbox（?q=<query>&t=all），
// 支持代码前缀、汉字名称、拼音缩写（"maotai" / "gzmt" → 贵州茅台）。
// 失败返回 nil，调用方按空列表处理（搜索是体验加分项，不该让主流程报错）。
func Search(ctx context.Context, query string, limit int) []SearchResult {
	q := strings.TrimSpace(query)
	if q == "" || limit <= 0 {
		return nil
	}

	// url.Values 编码：中文 / 空格转成 %XX，否则腾讯可能把请求当乱码拒绝
	params := url.Values{}
	params.Set("q", q)
	params.Set("t", "all")
	searchURL := smartBoxURL + "?" + params.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, searchURL, nil)
	if err != nil {
		log.Printf("build search request failed: %v", err)
		return nil
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (stock-agent-miniapp)")

	resp, err := searchHTTP.Do(req)
	if err != nil {
		log.Printf("search request failed: q=%s err=%v", q, err)
		return nil
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("search api http %d: q=%s", resp.StatusCode, q)
		return nil
	}

	// smartbox 返回纯 ASCII 文本（中文为 \uXXXX 转义），与行情接口的 GBK 字节不同，
	// 不能套字符解码；读取同样限长，防上游返回超大内容
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxSearchBodyBytes))
	if err != nil {
		log.Printf("read search body failed: %v", err)
		return nil
	}

	parsed := parseSmartBox(string(body), limit)
	// 无结果时留痕：便于区分「第三方确实没有」与「解析规则不匹配」
	if len(parsed) == 0 {
		log.Printf("search parsed 0 items: q=%s status=%d raw=%.200s", q, resp.StatusCode, string(body))
	}
	return parsed
}

// parseSmartBox 解析腾讯 smartbox 返回，样本形如：
//
//	v_hint="sh~600600~\u9752\u5c9b\u5564\u9152~qdpj~GP-A^sz~000600~...~jtny~GP-A^hk~...~GP^jj~...~KJ-HB";
//
// 按 ^ 分条、按 ~ 分字段：[0] market（sh/sz/bj/hk/jj）、[1] code、[2] name（\uXXXX）、
// [3] 拼音缩写、[4] type（GP-A 股票 / GP 港股 / KJ-HB 基金）。
//
// 三点易踩的坑（与行情接口完全不同，故用真实样本固化）：
//  1. 字段以 market 打头，不是 code；
//  2. 中文是 Unicode 转义文本，绝不能套 GBK 解码器；
//  3. 会混入港股、基金，必须按 market 过滤，只留 A 股。
func parseSmartBox(body string, limit int) []SearchResult {
	out := make([]SearchResult, 0, limit)

	m := varAssignRe.FindStringSubmatch(body)
	if m == nil {
		return out
	}
	hint := m[1]

	for _, line := range strings.Split(hint, "^") {
		if len(out) >= limit {
			break
		}
		fields := strings.Split(line, "~")
		if len(fields) < 3 {
			continue
		}

		market := strings.ToLower(strings.TrimSpace(fields[0]))
		code := strings.TrimSpace(fields[1])
		name := decodeUnicodeEscape(strings.TrimSpace(fields[2]))

		// 只要 A 股三个市场，过滤掉港股 / 基金 / 债券等
		if market != "sh" && market != "sz" && market != "bj" {
			continue
		}
		// A 股代码固定 6 位数字
		if len(code) != 6 || !isAllDigits(code) {
			continue
		}
		// type 存在时必须是股票（GP / GP-A），排除基金 KJ-* 等
		if len(fields) >= 5 {
			tp := strings.ToUpper(strings.TrimSpace(fields[4]))
			if tp != "" && !strings.HasPrefix(tp, "GP") {
				continue
			}
		}
		if name == "" {
			name = code
		}

		out = append(out, SearchResult{
			Code:   code,
			Market: strings.ToUpper(market),
			Name:   name,
		})
	}
	return out
}

// decodeUnicodeEscape 把 \uXXXX 还原成真实字符。
// 代理对（\ud83d\ude00 这类 emoji）必须先合并高低位再转 rune，
// 单个解出来是无效码元，会变成替换字符。
func decodeUnicodeEscape(s string) string {
	locs := unicodeEscape.FindAllStringIndex(s, -1)
	if len(locs) == 0 {
		return s
	}

	var b strings.Builder
	b.Grow(len(s))
	pos := 0
	for i := 0; i < len(locs); i++ {
		start, end := locs[i][0], locs[i][1]
		b.WriteString(s[pos:start])
		esc := s[start:end]

		r, err := decodeEscRune(esc)
		if err != nil {
			b.WriteString(esc) // 解不开就原样保留，至少不丢内容
			pos = end
			continue
		}
		if utf16.IsSurrogate(r) {
			// 高代理：与紧邻的下一个转义拼成代理对（emoji 等）
			if r <= 0xDBFF && i+1 < len(locs) && locs[i+1][0] == end {
				low, err2 := decodeEscRune(s[locs[i+1][0]:locs[i+1][1]])
				if err2 == nil && low >= 0xDC00 && low <= 0xDFFF {
					b.WriteRune(utf16.DecodeRune(r, low))
					pos = locs[i+1][1]
					i++ // 低代理已消费
					continue
				}
			}
			pos = end // 拼不上的孤立代理元，直接丢弃
			continue
		}
		b.WriteRune(r)
		pos = end
	}
	b.WriteString(s[pos:])
	return b.String()
}

// decodeEscRune 解单个 \uXXXX 转义成码元
func decodeEscRune(esc string) (rune, error) {
	n, err := strconv.ParseUint(esc[2:], 16, 32)
	if err != nil {
		return 0, err
	}
	return rune(n), nil
}

func isAllDigits(s string) bool {
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
