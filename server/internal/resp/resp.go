package resp

import (
	"encoding/json"
	"net/http"

	"github.com/gin-gonic/gin"
)

// 业务码：0 表示成功，其余与 HTTP 状态码对齐
const (
	CodeOK              = 0
	CodeBadRequest      = 400
	CodeUnauthorized    = 401
	CodeNotFound        = 404
	CodeConflict        = 409
	CodeTooManyRequests = 429
	CodeInternal        = 500
	CodeBadGateway      = 502
)

// Body 统一响应体：{ code, message, data }
type Body struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data"`
}

// Resp 统一响应入口，中间件与 Handler 共用，保证结构一致；
// 是否中断请求由调用方决定（中间件里自行 c.Abort()）。
func Resp(c *gin.Context, code int, msg string, data any) {
	c.JSON(Status(code), Body{Code: code, Message: msg, Data: data})
}

func OK(c *gin.Context, data any) { Resp(c, CodeOK, "ok", data) }

func Fail(c *gin.Context, code int, msg string) { Resp(c, code, msg, nil) }

// JSONBytes 序列化统一响应体，用于「同一份响应既写缓存又返回」的场景（如股票搜索），
// 保证缓存内容与真实响应逐字节一致，避免两边结构漂移。
func JSONBytes(code int, msg string, data any) ([]byte, error) {
	return json.Marshal(Body{Code: code, Message: msg, Data: data})
}

// Status 业务码 → HTTP 状态码；code=0 一律 200。
func Status(code int) int {
	switch code {
	case CodeOK:
		return http.StatusOK
	case CodeUnauthorized:
		return http.StatusUnauthorized
	case CodeBadRequest, CodeNotFound, CodeConflict:
		return code
	case CodeTooManyRequests:
		return http.StatusTooManyRequests
	case CodeBadGateway:
		return http.StatusBadGateway
	default:
		return http.StatusInternalServerError
	}
}
