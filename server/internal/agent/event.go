package agent

// Event 统一事件协议：Python Agent 产出，网关封装为 SSE 帧透传。
type Event struct {
	Event string `json:"event"`
	Data  any    `json:"data"`
}

// 预定义事件类型，与 Python Agent 侧约定一致；网关只透传，不解释 data。
// 其余事件名（token / card / table / sources / tool_start / tool_end / done）仅作字符串转发。
const (
	EventStatus = "status" // 运行状态提示，如「正在思考…」
	EventError  = "error"  // 结构化错误，不断连
)

// ErrorData 错误事件载荷；code 供前端分支处理（限流/配额/上游故障），msg 供展示。
type ErrorData struct {
	Code string `json:"code"`
	Msg  string `json:"msg"`
}

// RunRequest 一次运行的入参。
type RunRequest struct {
	Query     string `json:"query"`
	SessionID string `json:"session_id"`
}
