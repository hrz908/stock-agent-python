package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"

	"stock-agent-server/internal/resp"
)

// sseWriteTimeoutExtra SSE 写截止时间余量：server 级 WriteTimeout（默认 15s）会掐断长流，
// 这里借用 ResponseController 把本连接放宽到「单次运行上限 + 余量」。
const sseWriteTimeoutExtra = 15 * time.Second

// maxQueryRunes 单次提问字符上限：超过后每次 LLM 调用都要为同样的长文本付费，
// 且 64KB 请求体上限挡不住「一条 6 万字问题」把上下文顶爆。
const maxQueryRunes = 2000

// ServeSSE 处理 POST /api/v1/agent/stream（小程序与 H5 共用）。
// 请求体 {"query":"...","session_id":"..."}，响应 text/event-stream，
// 每帧 event: {type}\ndata: {json}\n\n 并立即 Flush。
func ServeSSE(c *gin.Context) {
	userID := c.GetInt64("user_id")

	// 这里只该收到一条提问的 JSON：不设上限时 20MB 的请求体会被整段读进内存
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 64<<10)

	var req RunRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		resp.Fail(c, resp.CodeBadRequest, "请求体格式错误")
		return
	}
	if strings.TrimSpace(req.Query) == "" {
		resp.Fail(c, resp.CodeBadRequest, "query 不能为空")
		return
	}
	if utf8.RuneCountInString(strings.TrimSpace(req.Query)) > maxQueryRunes {
		resp.Fail(c, resp.CodeBadRequest, fmt.Sprintf("提问过长，最多 %d 字", maxQueryRunes))
		return
	}

	ctx := c.Request.Context()

	// 流尚未开始，可直接回标准 429，便于前端与监控区分「被限流」与「流内错误」；
	// 对外只给中文提示，内部错误串留在服务端
	if err := limiter.Allow(ctx, userID); err != nil {
		switch {
		case errors.Is(err, ErrTooManyConcurrent):
			resp.Fail(c, resp.CodeTooManyRequests, "当前同时进行的对话过多，请稍后再试")
		case errors.Is(err, ErrQuotaExceeded):
			resp.Fail(c, resp.CodeTooManyRequests, "今日提问次数已用完，明天再来吧")
		default:
			resp.Fail(c, resp.CodeTooManyRequests, "请求过于频繁，请稍后再试")
		}
		return
	}
	defer limiter.ReleaseConcurrent(ctx, userID)

	// X-Accel-Buffering: no 关闭 Nginx 缓冲，否则事件被攒批下发、失去实时性
	c.Writer.Header().Set("Content-Type", "text/event-stream")
	c.Writer.Header().Set("Cache-Control", "no-cache")
	c.Writer.Header().Set("Connection", "keep-alive")
	c.Writer.Header().Set("X-Accel-Buffering", "no")
	c.Writer.WriteHeader(http.StatusOK)

	flusher, ok := c.Writer.(http.Flusher)
	if !ok {
		log.Printf("agent sse: response writer does not support flush")
		return
	}

	// 放宽写截止避免被 WriteTimeout 中断；gin 的 Writer 实现了 Unwrap，可透传到底层连接
	rc := http.NewResponseController(c.Writer)
	if err := rc.SetWriteDeadline(time.Now().Add(proxy.runTimeout + sseWriteTimeoutExtra)); err != nil {
		log.Printf("agent sse: set write deadline failed: %v", err)
	}

	threadID := fmt.Sprintf("default:%d:%s", userID, strings.TrimSpace(req.SessionID))
	events, errs := proxy.StreamRun(ctx, threadID, strings.TrimSpace(req.SessionID), req.Query, strconv.FormatInt(userID, 10))

	for {
		select {
		case ev, ok := <-events:
			if !ok {
				// 事件流结束：非取消类错误补发一帧 error，让前端知晓失败原因
				if err := <-errs; err != nil && !errors.Is(err, context.Canceled) {
					writeSSE(c, flusher, Event{Event: EventError, Data: ErrorData{Code: "agent_error", Msg: err.Error()}})
				}
				return
			}
			writeSSE(c, flusher, ev)
		case <-ctx.Done():
			// 客户端断开：上游请求随 ctx 取消
			return
		}
	}
}

// writeSSE 写出一帧并立即 Flush；data 为紧凑 JSON，单行即可。
func writeSSE(c *gin.Context, flusher http.Flusher, ev Event) {
	data, err := json.Marshal(ev.Data)
	if err != nil {
		data = []byte("null") // 序列化失败降级为 null，不中断整条流
	}
	_, _ = fmt.Fprintf(c.Writer, "event: %s\ndata: %s\n\n", ev.Event, data)
	flusher.Flush()
}
