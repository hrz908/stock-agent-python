package stock

import "github.com/gin-gonic/gin"

// Register 把本模块路由挂到 /api/v1 下；鉴权中间件由调用方挂（router 的 authorized 组）
func Register(g *gin.RouterGroup) {
	g.GET("/stocks/search", SearchStock)
	g.GET("/stocks/profile", GetProfile)
	g.GET("/stock/quote", GetQuote)
	g.GET("/stock/kline", GetKline)
}
