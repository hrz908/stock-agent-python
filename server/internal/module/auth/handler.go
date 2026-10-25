// 包名取 authapi，与 internal/auth（JWT 签发/解析）区分开，
// 这个包是「登录接口」业务模块
package authapi

import (
	"context"
	"errors"
	"log"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"stock-agent-server/internal/auth"
	"stock-agent-server/internal/deps"
	"stock-agent-server/internal/model"
	"stock-agent-server/internal/resp"
	"stock-agent-server/internal/wechat"
)

type LoginReq struct {
	Code string `json:"code"` // 小程序 wx.login 拿到的 code
}

type LoginResp struct {
	Token    string `json:"token"`
	UserID   int64  `json:"user_id"`
	Nickname string `json:"nickname"`
	Avatar   string `json:"avatar"`
	// IsNew 是否本次刚建的号，前端据此分流：
	// false 且已有资料 → 直接登录不打扰；true → 才引导补头像昵称
	IsNew bool `json:"is_new"`
}

// Login POST /api/v1/auth/login
// 只做身份认证：code → openid → 查/建用户 → 签发 JWT，并回传库里已有资料。
// 不接收 nickname/avatar：那是微信的用户权限数据，得先拿到身份、确认库里没有才值得让用户授权；
// 补齐资料走 PUT /user/profile（那时已有 token）
func Login(c *gin.Context) {
	ctx := c.Request.Context()

	// 登录是唯一公开的写接口：请求体只该有一个几十字节的 code
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 4<<10)

	var req LoginReq
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.Code) == "" {
		resp.Fail(c, resp.CodeBadRequest, "缺少登录 code")
		return
	}
	if len(req.Code) > 128 {
		resp.Fail(c, resp.CodeBadRequest, "登录 code 不合法")
		return
	}

	openid, err := openIDOf(ctx, req.Code)
	if err != nil {
		log.Printf("login resolve openid failed: %v", err)
		resp.Fail(c, resp.CodeBadGateway, "微信登录失败，请重试")
		return
	}

	u, isNew, err := findOrCreateUser(ctx, openid)
	if err != nil {
		log.Printf("login upsert user failed: openid=%s err=%v", openid, err)
		resp.Fail(c, resp.CodeInternal, "登录失败")
		return
	}

	token, err := auth.SignToken(u.ID)
	if err != nil {
		log.Printf("sign token failed: user=%d err=%v", u.ID, err)
		resp.Fail(c, resp.CodeInternal, "登录失败")
		return
	}

	resp.OK(c, LoginResp{
		Token:    token,
		UserID:   u.ID,
		Nickname: u.Nickname,
		Avatar:   u.Avatar,
		IsNew:    isNew,
	})
}

// openIDOf code → openid。
// 配了 AppID/Secret 就走微信；没配则退化为固定演示账号，
// 保证本地没接微信时也能把链路（登录 → 建号 → 存资料）跑通
func openIDOf(ctx context.Context, code string) (string, error) {
	if wechat.Enabled() {
		session, err := wechat.Code2Session(ctx, strings.TrimSpace(code))
		if err != nil {
			return "", err
		}
		return session.OpenID, nil
	}
	// 演示账号：所有人共用同一个 openid，方便本地联调；
	// prod 由 main 强制要求微信凭据，不会走到这里
	return "demo", nil
}

// findOrCreateUser 按 openid 查用户，没有就建（返回 isNew=true）。
// 已有用户原样返回、一个字段都不动——重复登录不该改动用户自己设好的资料。
// 新建时昵称用默认值占位，真正的值等用户授权后由 PUT /user/profile 覆盖，
// 该默认值同时是前端判断「资料未完善」的依据（与前端 DEFAULT_NICKNAME 一致）
func findOrCreateUser(ctx context.Context, openid string) (*model.User, bool, error) {
	var u model.User
	err := deps.DB.WithContext(ctx).Where("openid = ?", openid).First(&u).Error

	switch {
	case err == nil:
		return &u, false, nil

	case errors.Is(err, gorm.ErrRecordNotFound):
		// 并发首次登录时两个请求都会走到这里：用 OnConflict DoNothing 让后到者静默跳过
		// （撞 uk_users_openid），再查一次拿回已有行，否则它会拿到 500
		nu := model.User{OpenID: openid, Nickname: "微信用户"}
		if err := deps.DB.WithContext(ctx).
			Clauses(clause.OnConflict{Columns: []clause.Column{{Name: "openid"}}, DoNothing: true}).
			Create(&nu).Error; err != nil {
			return nil, false, err
		}
		if nu.ID == 0 {
			// 冲突了：行已存在，重新查出来
			if err := deps.DB.WithContext(ctx).Where("openid = ?", openid).First(&nu).Error; err != nil {
				return nil, false, err
			}
			return &nu, false, nil
		}
		return &nu, true, nil

	default:
		return nil, false, err
	}
}
