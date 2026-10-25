package agent

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"stock-agent-server/internal/httpclient"
)

// AgentProxy 代理到 Python Agent 服务：把一次运行转换为流式事件序列。
type AgentProxy struct {
	baseURL string
	client  *http.Client
	sem     chan struct{} // 有界信号量，限制上游并发

	// runTimeout 单次运行总超时（默认 120s），到点取消上游，避免连接长期悬挂
	runTimeout time.Duration
	// ttftTimeout 首字节等待上限；超时先推 status 事件，避免前端久无反馈
	ttftTimeout time.Duration
	// maxLineSize 单行 ndjson 上限（默认 4MB），超出视为上游异常，防撑爆内存
	maxLineSize int
}

// NewAgentProxy 创建代理。不设 http.Client.Timeout：流读取时长不可预估，
// 超时统一由 runCtx 控制，避免长流被掐断；Transport 复用空闲连接，避免每次运行重走握手。
func NewAgentProxy(opts Options) *AgentProxy {
	return &AgentProxy{
		baseURL:     strings.TrimRight(opts.BaseURL, "/"),
		client:      httpclient.New(0),
		sem:         make(chan struct{}, opts.ProxyConcurrency),
		runTimeout:  opts.RunTimeout,
		ttftTimeout: opts.TTFTTimeout,
		maxLineSize: opts.MaxLineBytes,
	}
}

// StreamRun 发起一次流式运行，返回事件通道与错误通道
// （goroutine 结束时两者均关闭，errs 至多一个值）；取消 ctx 会提前终止并取消上游请求。
// sessionID 仅用于会话注册（registerSession），失败不阻断主流程。
func (p *AgentProxy) StreamRun(ctx context.Context, threadID, sessionID, query, userID string) (<-chan Event, <-chan error) {
	// 缓冲取小值：背压本来就是期望行为，缓冲越大越容易把大卡片事件堆在内存里
	events := make(chan Event, 8)
	errs := make(chan error, 1)

	// 会话注册是旁路逻辑：异步执行并脱离请求 ctx。
	// 同步跑会让每个提问在首个 token 前多等一次上游往返（最多 2s），
	// 用 WithoutCancel 则客户端断开也能把标题写进去
	go p.registerSession(context.WithoutCancel(ctx), sessionID, userID, query)

	go func() {
		// 关闭顺序 LIFO：先 errs 后 events，
		// 消费方读到 events 关闭时错误值必已就位
		defer close(events)
		defer close(errs)

		// 占上游并发槽；ctx 已取消则直接退出
		select {
		case p.sem <- struct{}{}:
			defer func() { <-p.sem }()
		case <-ctx.Done():
			errs <- ctx.Err()
			return
		}

		runCtx, cancel := context.WithTimeout(ctx, p.runTimeout)
		defer cancel()

		payload, err := json.Marshal(map[string]string{
			"query":     query,
			"thread_id": threadID,
			"user_id":   userID,
		})
		if err != nil {
			errs <- err
			return
		}

		req, err := http.NewRequestWithContext(runCtx, http.MethodPost, p.baseURL+"/run/stream", bytes.NewReader(payload))
		if err != nil {
			errs <- err
			return
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "application/x-ndjson")

		resp, err := p.client.Do(req)
		if err != nil {
			errs <- err
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode != http.StatusOK {
			// 带出上游错误正文便于定位，限制读取长度防超大响应拖累
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
			errs <- fmt.Errorf("agent service returned status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
			return
		}

		p.pumpNDJSON(runCtx, resp.Body, events, errs)
	}()

	return events, errs
}

// pumpNDJSON 逐行读 ndjson 并转发为 Event。
// 扫描放在独立 goroutine，主循环才能同时 select 行数据 / TTFT 定时器 / ctx 取消。
func (p *AgentProxy) pumpNDJSON(runCtx context.Context, body io.Reader, events chan<- Event, errs chan<- error) {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 64*1024), p.maxLineSize)

	lines := make(chan []byte)
	scanErr := make(chan error, 1)
	go func() {
		defer close(lines)
		for scanner.Scan() {
			// Scanner.Bytes 复用底层数组，必须拷贝后再投递
			line := append([]byte(nil), scanner.Bytes()...)
			select {
			case lines <- line:
			case <-runCtx.Done():
				scanErr <- runCtx.Err()
				return
			}
		}
		scanErr <- scanner.Err()
	}()

	ttft := time.NewTimer(p.ttftTimeout)
	defer ttft.Stop()
	firstReceived := false

	for {
		select {
		case line, ok := <-lines:
			if !ok {
				// 扫描结束，取回扫描错误（EOF 时为 nil）
				if err := <-scanErr; err != nil && !errors.Is(err, context.Canceled) {
					errs <- err
				}
				return
			}
			if !firstReceived {
				firstReceived = true
				ttft.Stop() // 首字节已到，撤销 TTFT 提示
			}
			if len(bytes.TrimSpace(line)) == 0 {
				continue // ndjson 允许行间空行
			}
			var ev Event
			if err := json.Unmarshal(line, &ev); err != nil {
				// 单行损坏不中断整条流
				log.Printf("agent proxy: malformed ndjson line skipped: %v", err)
				continue
			}
			select {
			case events <- ev:
			case <-runCtx.Done():
				errs <- runCtx.Err()
				return
			}

		case <-ttft.C:
			// 首字节超时：推一次 status 安抚前端（定时器只触发一次，不会刷屏）
			if !firstReceived {
				select {
				case events <- Event{Event: EventStatus, Data: map[string]string{"stage": "thinking", "label": "正在思考…"}}:
				case <-runCtx.Done():
					errs <- runCtx.Err()
					return
				}
			}

		case <-runCtx.Done():
			// 客户端断开或整体超时：扫描 goroutine 随 runCtx 退出
			errs <- runCtx.Err()
			return
		}
	}
}
