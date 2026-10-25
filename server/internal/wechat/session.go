// Package wechat 封装小程序侧开放接口，目前只有 code2session：
// 用 wx.login 拿到的 code 换 openid。
package wechat

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"stock-agent-server/internal/httpclient"
)

const code2SessionURL = "https://api.weixin.qq.com/sns/jscode2session"

// AppID / Secret 由 main 通过 Init 注入，运行期不再读取；
// 两者都为空表示「未接入微信」，登录退化成演示账号（见 module/auth）。
var (
	AppID  string
	Secret string
	// httpClient 复用连接：登录是高频短请求，省掉每次 TLS 握手
	httpClient = httpclient.New(5 * time.Second)
	// httpTimeout 请求级超时，与 httpClient.Timeout 同档；由 Init 按配置覆盖
	httpTimeout = 5 * time.Second
)

// Init 注入小程序凭据与上游超时，main 启动时调用一次。
func Init(appID, secret string, timeout time.Duration) {
	AppID = appID
	Secret = secret
	if timeout > 0 {
		httpTimeout = timeout
		httpClient = httpclient.New(timeout)
	}
}

// Session 微信登录态；只保留用到的 openid，unionid / session_key 全仓无读取方。
type Session struct {
	OpenID string
}

type code2SessionResp struct {
	OpenID  string `json:"openid"`
	ErrCode int    `json:"errcode"`
	ErrMsg  string `json:"errmsg"`
}

// maxRespBytes 微信响应体上限：jscode2session 只回一小段 JSON，防上游异常时超大响应打爆内存。
const maxRespBytes = 64 << 10

// Enabled 是否已配置凭据；未配置时不要调微信接口，否则每个登录请求都白等一次网络失败。
func Enabled() bool { return AppID != "" && Secret != "" }

// Code2Session 用 code 换 openid。
// 微信的错误写在 200 响应体的 errcode 里，不能只看 HTTP 状态码。
// ctx 来自请求：客户端断开时上游调用随之取消，不白占一次 5s。
func Code2Session(ctx context.Context, code string) (*Session, error) {
	if !Enabled() {
		return nil, errors.New("wechat appid/secret not configured")
	}

	q := url.Values{}
	q.Set("appid", AppID)
	q.Set("secret", Secret)
	q.Set("js_code", code)
	q.Set("grant_type", "authorization_code")

	// 请求级 ctx 超时兜底：即使 Client.Timeout 未生效，单次调用也不会无限期挂起
	ctx, cancel := context.WithTimeout(ctx, httpTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, code2SessionURL+"?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	var out code2SessionResp
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxRespBytes)).Decode(&out); err != nil {
		return nil, err
	}
	if out.ErrCode != 0 || strings.TrimSpace(out.OpenID) == "" {
		return nil, fmt.Errorf("jscode2session failed: errcode=%d errmsg=%s", out.ErrCode, strings.TrimSpace(out.ErrMsg))
	}

	return &Session{OpenID: out.OpenID}, nil
}
