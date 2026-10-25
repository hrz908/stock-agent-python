package quickask

import "github.com/gin-gonic/gin"

// Register 把首屏建议问题挂到 /api/v1 下。
// 挂在行情限流组里（router 侧决定）：池子过期时这个接口会批量拉上游行情。
func Register(g *gin.RouterGroup) {
	g.GET("/quick-asks", List)
	g.POST("/quick-asks/click", Click)
}
