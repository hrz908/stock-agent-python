package user

import (
	"github.com/gin-gonic/gin"
)

// Register 注册本模块路由；四个接口都要登录，group 由 router 传入已挂 auth 的组
func Register(g *gin.RouterGroup) {
	g.GET("/user/profile", GetProfile)
	g.PUT("/user/profile", UpdateProfile)
	g.POST("/user/avatar", UploadAvatar)
	// 账号注销：合规要求（用户必须能删除自己的账号与数据），删除后不可恢复
	g.DELETE("/user/account", DeleteAccount)
}
