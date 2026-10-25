package agent

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"stock-agent-server/internal/resp"
)

// 会话历史网关接口的测试：用 httptest 起假 Python Agent，
// 验证「鉴权取 user_id → 转发 → 统一响应体」链路，不依赖真实 Postgres / Redis / LangGraph。

// upstream 记录假 Agent 收到的最后一次请求，供断言转发参数
type upstream struct {
	method string
	path   string
	query  string
	body   string
	hits   int
}

// startFakeAgent 起一个假的 Python Agent，behavior 决定它的响应。
// 返回服务与请求记录（指针，供用例读取）。
func startFakeAgent(t *testing.T, behavior http.HandlerFunc) (*httptest.Server, *upstream) {
	t.Helper()
	rec := &upstream{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.hits++
		rec.method = r.Method
		rec.path = r.URL.Path
		rec.query = r.URL.RawQuery
		b, _ := io.ReadAll(r.Body)
		rec.body = strings.TrimSpace(string(b))
		behavior(w, r)
	}))
	t.Cleanup(srv.Close)

	// 处理器取的是包级单例 proxy，这里指向假上游；用完全局还原，避免污染其它用例
	old := proxy
	proxy = NewAgentProxy(Options{
		BaseURL:          srv.URL,
		ProxyConcurrency: 4,
		RunTimeout:       2 * time.Second,
		TTFTTimeout:      time.Second,
		MaxLineBytes:     1 << 20,
	})
	t.Cleanup(func() { proxy = old })
	return srv, rec
}

// newTestEngine 造一个注入了 user_id 的 gin 引擎（Auth 中间件在单测里被跳过，
//
//	handlers 只依赖 c.GetInt64("user_id")）
func newTestEngine() *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Set("user_id", int64(42))
		c.Next()
	})
	r.GET("/api/v1/agent/sessions", ServeListSessions)
	r.POST("/api/v1/agent/sessions", ServeCreateSession)
	r.GET("/api/v1/agent/sessions/:sid/messages", ServeSessionMessages)
	r.DELETE("/api/v1/agent/sessions/:sid", ServeDeleteSession)
	return r
}

// do 发起一次请求并返回解析后的统一响应体
func do(t *testing.T, r *gin.Engine, method, path string, body string, ctx context.Context) (int, resp.Body) {
	t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, path, reader)
	req.Header.Set("Content-Type", "application/json")
	if ctx != nil {
		req = req.WithContext(ctx)
	}
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	var out resp.Body
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

// dataOf 把 data 字段再解一层（data 是 any，Unmarshal 后是 map）
func dataOf(t *testing.T, b resp.Body) map[string]any {
	t.Helper()
	raw, err := json.Marshal(b.Data)
	if err != nil {
		t.Fatalf("marshal data: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("unmarshal data: %v", err)
	}
	return m
}

func okJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// ===== POST /api/v1/agent/sessions：注册新对话（「新对话」按钮）=====

func TestServeCreateSession_OK(t *testing.T) {
	_, rec := startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		okJSON(w, map[string]any{"ok": true})
	})
	r := newTestEngine()

	status, body := do(t, r, http.MethodPost, "/api/v1/agent/sessions",
		`{"session_id":"sess_1761_abc","title":""}`, nil)

	if status != http.StatusOK || body.Code != resp.CodeOK {
		t.Fatalf("期望 200/code=0，实际 %d/%+v", status, body)
	}
	if got := dataOf(t, body)["session_id"]; got != "sess_1761_abc" {
		t.Fatalf("session_id 应原样回传，实际 %v", got)
	}
	// 转发给 Agent 的必须是「网关从 JWT 解析出来的 user_id」，不是请求体里的
	if rec.method != http.MethodPost || rec.path != "/sessions" {
		t.Fatalf("转发方法/路径错误：%s %s", rec.method, rec.path)
	}
	var sent map[string]string
	if err := json.Unmarshal([]byte(rec.body), &sent); err != nil {
		t.Fatalf("转发体不是合法 JSON：%v（%s）", err, rec.body)
	}
	if sent["user_id"] != "42" || sent["session_id"] != "sess_1761_abc" || sent["title"] != "" {
		t.Fatalf("转发参数错误：%+v", sent)
	}
}

func TestServeCreateSession_TrimAndValidate(t *testing.T) {
	_, rec := startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		okJSON(w, map[string]any{"ok": true})
	})
	r := newTestEngine()

	// 前后空白应被 trim（前端生成 id 不会有，但手改参数/其它端可能有）
	status, body := do(t, r, http.MethodPost, "/api/v1/agent/sessions",
		`{"session_id":"  sess_1  ","title":"  新对话  "}`, nil)
	if status != http.StatusOK || body.Code != resp.CodeOK {
		t.Fatalf("trim 后应合法，实际 %d/%+v", status, body)
	}
	var sent map[string]string
	_ = json.Unmarshal([]byte(rec.body), &sent)
	if sent["session_id"] != "sess_1" || sent["title"] != "新对话" {
		t.Fatalf("trim 未生效：%+v", sent)
	}
}

func TestServeCreateSession_BadRequest(t *testing.T) {
	_, rec := startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		// 上游 goroutine 里不能调 t.Fatal（会 Goexit 掉错的协程），用 Errorf 记账
		t.Errorf("请求体非法时不应打到上游：%s %s", r.Method, r.URL.Path)
		okJSON(w, map[string]any{"ok": true})
	})
	r := newTestEngine()

	cases := []struct {
		name string
		body string
		msg  string
	}{
		{"非 JSON", `{"session_id":`, "请求体格式错误"},
		{"缺 session_id", `{"title":"x"}`, "session_id 不合法"},
		{"含非法字符", `{"session_id":"sess 1/../x"}`, "session_id 不合法"},
		{"超长", `{"session_id":"` + strings.Repeat("a", 65) + `"}`, "session_id 不合法"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			status, body := do(t, r, http.MethodPost, "/api/v1/agent/sessions", c.body, nil)
			if status != http.StatusBadRequest || body.Code != resp.CodeBadRequest {
				t.Fatalf("期望 400/400，实际 %d/%+v", status, body)
			}
			if body.Message != c.msg {
				t.Fatalf("提示语错误：%q", body.Message)
			}
		})
	}
	if rec.hits != 0 {
		t.Fatalf("非法请求不应转发给上游，实际转发 %d 次", rec.hits)
	}
}

func TestServeCreateSession_UpstreamError(t *testing.T) {
	startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("db down"))
	})
	r := newTestEngine()

	status, body := do(t, r, http.MethodPost, "/api/v1/agent/sessions",
		`{"session_id":"sess_1"}`, nil)
	if status != http.StatusBadGateway || body.Code != resp.CodeBadGateway {
		t.Fatalf("上游 500 应转成 502，实际 %d/%+v", status, body)
	}
	if body.Message != "会话创建失败" {
		t.Fatalf("提示语应为「会话创建失败」，实际 %q", body.Message)
	}
}

func TestServeCreateSession_UpstreamTimeout(t *testing.T) {
	startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(400 * time.Millisecond) // 比用例的 ctx 超时更久
		okJSON(w, map[string]any{"ok": true})
	})
	r := newTestEngine()

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	status, body := do(t, r, http.MethodPost, "/api/v1/agent/sessions",
		`{"session_id":"sess_1"}`, ctx)
	if status != http.StatusBadGateway || body.Code != resp.CodeBadGateway {
		t.Fatalf("上游超时应转成 502，实际 %d/%+v", status, body)
	}
}

// ===== GET /api/v1/agent/sessions：历史对话列表 =====

func TestServeListSessions(t *testing.T) {
	_, rec := startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		okJSON(w, map[string]any{"sessions": []map[string]string{
			{"session_id": "sess_2", "title": "茅台现在多少钱", "updated_at": "2026-10-24T10:00:00+08:00"},
			{"session_id": "sess_1", "title": "新对话", "updated_at": "2026-10-24T09:00:00+08:00"},
		}})
	})
	r := newTestEngine()

	status, body := do(t, r, http.MethodGet, "/api/v1/agent/sessions", "", nil)
	if status != http.StatusOK || body.Code != resp.CodeOK {
		t.Fatalf("期望 200/0，实际 %d/%+v", status, body)
	}
	if rec.query != "user_id=42" {
		t.Fatalf("user_id 应通过 query 传给上游，实际 %q", rec.query)
	}

	raw, _ := json.Marshal(dataOf(t, body)["sessions"])
	var list []sessionItem
	if err := json.Unmarshal(raw, &list); err != nil {
		t.Fatalf("列表结构不符合前端约定：%v", err)
	}
	if len(list) != 2 || list[0].SessionID != "sess_2" || list[1].Title != "新对话" {
		t.Fatalf("列表内容/顺序错误：%+v", list)
	}
}

func TestServeListSessions_UpstreamError(t *testing.T) {
	startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	})
	r := newTestEngine()

	status, body := do(t, r, http.MethodGet, "/api/v1/agent/sessions", "", nil)
	if status != http.StatusBadGateway || body.Message != "会话列表获取失败" {
		t.Fatalf("期望 502/会话列表获取失败，实际 %d/%+v", status, body)
	}
}

// ===== GET /api/v1/agent/sessions/:sid/messages：恢复上下文 =====

func TestServeSessionMessages(t *testing.T) {
	_, rec := startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		okJSON(w, map[string]any{"messages": []map[string]string{
			{"role": "user", "content": "茅台现在多少钱"},
			{"role": "assistant", "content": "1680.50 元"},
		}})
	})
	r := newTestEngine()

	status, body := do(t, r, http.MethodGet, "/api/v1/agent/sessions/sess_9/messages", "", nil)
	if status != http.StatusOK || body.Code != resp.CodeOK {
		t.Fatalf("期望 200/0，实际 %d/%+v", status, body)
	}
	if rec.path != "/sessions/sess_9/messages" || rec.query != "user_id=42" {
		t.Fatalf("转发路径/参数错误：%s?%s", rec.path, rec.query)
	}

	raw, _ := json.Marshal(dataOf(t, body)["messages"])
	var msgs []messageItem
	if err := json.Unmarshal(raw, &msgs); err != nil {
		t.Fatalf("消息结构不符合前端约定：%v", err)
	}
	if len(msgs) != 2 || msgs[0].Role != "user" || msgs[1].Content != "1680.50 元" {
		t.Fatalf("消息内容错误：%+v", msgs)
	}
}

func TestServeSessionMessages_InvalidID(t *testing.T) {
	_, rec := startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("非法 session_id 不应打到上游：%s", r.URL.Path)
		okJSON(w, map[string]any{"messages": []any{}})
	})
	r := newTestEngine()

	status, body := do(t, r, http.MethodGet, "/api/v1/agent/sessions/bad%20id/messages", "", nil)
	if status != http.StatusBadRequest || body.Message != "session_id 不合法" {
		t.Fatalf("期望 400/session_id 不合法，实际 %d/%+v", status, body)
	}
	if rec.hits != 0 {
		t.Fatalf("不应转发，实际 %d 次", rec.hits)
	}
}

// ===== DELETE /api/v1/agent/sessions/:sid =====

func TestServeDeleteSession(t *testing.T) {
	_, rec := startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		okJSON(w, map[string]any{"ok": true, "deleted": true})
	})
	r := newTestEngine()

	status, body := do(t, r, http.MethodDelete, "/api/v1/agent/sessions/sess_9", "", nil)
	if status != http.StatusOK || body.Code != resp.CodeOK {
		t.Fatalf("期望 200/0，实际 %d/%+v", status, body)
	}
	if got := dataOf(t, body)["deleted"]; got != true {
		t.Fatalf("deleted 应透传 true，实际 %v", got)
	}
	if rec.method != http.MethodDelete || rec.path != "/sessions/sess_9" || rec.query != "user_id=42" {
		t.Fatalf("转发错误：%s %s?%s", rec.method, rec.path, rec.query)
	}
}

func TestServeDeleteSession_NotFound(t *testing.T) {
	startFakeAgent(t, func(w http.ResponseWriter, r *http.Request) {
		okJSON(w, map[string]any{"ok": true, "deleted": false})
	})
	r := newTestEngine()

	status, body := do(t, r, http.MethodDelete, "/api/v1/agent/sessions/sess_none", "", nil)
	if status != http.StatusOK {
		t.Fatalf("删不存在的会话仍应 200（幂等），实际 %d", status)
	}
	if got := dataOf(t, body)["deleted"]; got != false {
		t.Fatalf("deleted 应为 false，实际 %v", got)
	}
}

// ===== 纯函数 =====

func TestValidSessionID(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"sess_1761234567890_k3j2h1a9z", true},
		{"a", true},
		{"A-B_9", true},
		{"", false},
		{strings.Repeat("a", 65), false},
		{"sess 1", false},
		{"sess/1", false},
		{"sess:1", false}, // 冒号会破坏 thread_id 的 default:user:session 结构
		{"中文", false},
	}
	for _, c := range cases {
		if got := validSessionID(c.in); got != c.want {
			t.Fatalf("validSessionID(%q) = %v，期望 %v", c.in, got, c.want)
		}
	}
}

func TestTruncateRunes(t *testing.T) {
	if got := truncateRunes("  这是一段很长的中文标题，用来验证按字符截断不会切出乱码  ", 6); got != "这是一段很长" {
		t.Fatalf("按字符截断错误：%q", got)
	}
	if got := truncateRunes("short", 40); got != "short" {
		t.Fatalf("短标题应原样返回：%q", got)
	}
}
