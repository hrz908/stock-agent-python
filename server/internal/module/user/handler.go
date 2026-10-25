// Package user 用户资料：查询、修改、头像上传。
// user_id 只从鉴权中间件注入的 context 取，不信任请求体里的任何身份字段
package user

import (
	"errors"
	"fmt"
	"io"
	"log"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"stock-agent-server/internal/deps"
	"stock-agent-server/internal/model"
	"stock-agent-server/internal/resp"
	"stock-agent-server/internal/upload"
)

// nicknameMax 昵称长度上限，与小程序侧 NICKNAME_MAX 一致；
// 按字符数算（中文一个字算一个），用 utf8.RuneCountInString 而不是 len
const nicknameMax = 10

// avatarMaxBytes 头像上限 2MB。多出的 1KB 余量给 multipart 头与边界：
// 实际写入字节数受这个总上限约束，否则客户端改一下头部 size 字段就能绕过校验
const avatarMaxBytes = 2<<20 + 1024

// avatarPrefix 头像在库中只允许存这个前缀下的站内路径：
// 客户端若传外链或 ../ 之类的路径，渲染时会被当成任意图片地址注入
const avatarPrefix = "/uploads/avatars/"

type ProfileResp struct {
	Nickname string `json:"nickname"`
	Avatar   string `json:"avatar"`
}

type UpdateProfileReq struct {
	Nickname string `json:"nickname"`
	Avatar   string `json:"avatar"`
}

// GetProfile GET /api/v1/user/profile
func GetProfile(c *gin.Context) {
	u, ok := current(c)
	if !ok {
		return
	}
	resp.OK(c, ProfileResp{Nickname: u.Nickname, Avatar: u.Avatar})
}

// UpdateProfile PUT /api/v1/user/profile
// 昵称超出上限时截断到 nicknameMax 个字符；avatar 传空串表示不改动
func UpdateProfile(c *gin.Context) {
	ctx := c.Request.Context()
	u, ok := current(c)
	if !ok {
		return
	}

	var req UpdateProfileReq
	if err := c.ShouldBindJSON(&req); err != nil {
		resp.Fail(c, resp.CodeBadRequest, "参数错误")
		return
	}

	nickname := strings.TrimSpace(req.Nickname)
	if nickname == "" {
		resp.Fail(c, resp.CodeBadRequest, "昵称不能为空")
		return
	}
	if utf8.RuneCountInString(nickname) > nicknameMax {
		nickname = trimRunes(nickname, nicknameMax)
	}

	updates := map[string]any{"nickname": nickname}
	avatar := u.Avatar
	if raw := strings.TrimSpace(req.Avatar); raw != "" {
		// 只能引用自己上传的文件：文件名带 user_id，允许任意路径等于能把别人的头像挂到自己名下
		if !isSafeAvatar(raw) || !strings.HasPrefix(filepath.Base(raw), fmt.Sprintf("avatar_%d_", u.ID)) {
			resp.Fail(c, resp.CodeBadRequest, "头像地址不合法")
			return
		}
		updates["avatar"] = raw
		avatar = raw
	}

	if err := deps.DB.WithContext(ctx).Model(&model.User{}).Where("id = ?", u.ID).Updates(updates).Error; err != nil {
		log.Printf("update profile failed: user=%d err=%v", u.ID, err)
		resp.Fail(c, resp.CodeInternal, "保存失败")
		return
	}

	// 换头像成功后再删旧文件：先删后写一旦写失败，用户就丢了自己的头像
	if avatar != u.Avatar && u.Avatar != "" {
		removeAvatarFile(u.Avatar)
	}

	resp.OK(c, ProfileResp{Nickname: nickname, Avatar: avatar})
}

// UploadAvatar POST /api/v1/user/avatar
// 小程序用 wx.uploadFile 发 multipart，字段名为 file；
// 返回站内相对路径，前端拼上后端基地址即可显示
func UploadAvatar(c *gin.Context) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")

	// 请求体总大小先卡住：file.Size 来自 multipart 头部、客户端可控，
	// 真正的限制要靠读取时不超量
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, avatarMaxBytes)

	file, err := c.FormFile("file")
	if err != nil {
		resp.Fail(c, resp.CodeBadRequest, "缺少头像文件")
		return
	}
	if file.Size > avatarMaxBytes {
		resp.Fail(c, resp.CodeBadRequest, "头像不能超过 2MB")
		return
	}

	// 只保留认识的扩展名，其余一律按 png 存，避免把可疑后缀写进磁盘
	ext := strings.ToLower(filepath.Ext(file.Filename))
	switch ext {
	case ".jpg", ".jpeg", ".png", ".webp":
	default:
		ext = ".png"
	}

	// 扩展名可以随便改，真正的类型看文件头：非图片内容一律拒绝，
	// 否则任意文件（HTML/脚本）会被当成图片存进公开静态目录
	if err := checkImageContent(file); err != nil {
		resp.Fail(c, resp.CodeBadRequest, "头像必须是 JPG/PNG/WebP 图片")
		return
	}

	name := fmt.Sprintf("avatar_%d_%d%s", userID, time.Now().UnixNano(), ext)
	if err := c.SaveUploadedFile(file, upload.AvatarPath(name)); err != nil {
		log.Printf("save avatar failed: user=%d err=%v", userID, err)
		resp.Fail(c, resp.CodeInternal, "头像保存失败")
		return
	}

	rel := avatarPrefix + name

	// 先记下旧头像：写库成功后再删，避免删了旧文件、新路径又没写进去
	var old model.User
	oldAvatar := ""
	if err := deps.DB.WithContext(ctx).Model(&model.User{}).
		Where("id = ?", userID).First(&old).Error; err == nil {
		oldAvatar = old.Avatar
	} else if !errors.Is(err, gorm.ErrRecordNotFound) {
		log.Printf("load old avatar failed: user=%d err=%v", userID, err)
	}

	if err := deps.DB.WithContext(ctx).Model(&model.User{}).Where("id = ?", userID).Update("avatar", rel).Error; err != nil {
		log.Printf("update avatar failed: user=%d err=%v", userID, err)
		// 回滚刚落盘的文件，别在磁盘上留一个没人引用的孤儿
		if rmErr := os.Remove(upload.AvatarPath(name)); rmErr != nil {
			log.Printf("rollback avatar file failed: name=%s err=%v", name, rmErr)
		}
		resp.Fail(c, resp.CodeInternal, "头像保存失败")
		return
	}

	if oldAvatar != "" && oldAvatar != rel {
		removeAvatarFile(oldAvatar)
	}

	resp.OK(c, gin.H{"avatar_url": rel})
}

// 注销时要清的两张业务表。只声明表名、不 import 对应模块（模块之间禁止互相依赖），
// 因此表名必须与 module/watchlist、module/alert 的 TableName() 保持一致；
// 字段不参与任何读写，DELETE 只按 user_id 过滤
type purgeWatchlist struct {
	ID int64 `gorm:"primaryKey;column:id"`
}

func (purgeWatchlist) TableName() string { return "user_watchlist" }

type purgeAlertRule struct {
	ID int64 `gorm:"primaryKey;column:id"`
}

func (purgeAlertRule) TableName() string { return "alert_rule" }

// DeleteAccount DELETE /api/v1/user/account
// 硬删当前用户及其业务数据：自选、提醒规则、用户行（含 openid / 昵称 / 头像路径），
// 并尽力删除磁盘上的头像文件。删完 openid 不再存在，同一微信再次登录会建一个新账号。
//
// 对话会话与 LangGraph checkpoint 由 Python Agent 侧持有，网关无法跨模块清理：
// 小程序在调用本接口前会先逐条 DELETE /agent/sessions/:sid，把能清的先清掉；
// 未清掉的部分只保留了旧 user_id，与个人信息已无关联。
func DeleteAccount(c *gin.Context) {
	ctx := c.Request.Context()
	u, ok := current(c)
	if !ok {
		return
	}

	err := deps.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Where("user_id = ?", u.ID).Delete(&purgeAlertRule{}).Error; err != nil {
			return err
		}
		if err := tx.Where("user_id = ?", u.ID).Delete(&purgeWatchlist{}).Error; err != nil {
			return err
		}
		return tx.Where("id = ?", u.ID).Delete(&model.User{}).Error
	})
	if err != nil {
		log.Printf("delete account failed: user=%d err=%v", u.ID, err)
		resp.Fail(c, resp.CodeInternal, "注销失败")
		return
	}

	// 库删成功后再删文件：先删文件、事务又回滚的话，用户头像就没了但账号还在
	removeAvatarFile(u.Avatar)

	resp.OK(c, gin.H{"deleted": true})
}

// current 取当前登录用户；取不到（未登录 / 用户不存在）时自行写回响应
func current(c *gin.Context) (*model.User, bool) {
	ctx := c.Request.Context()
	userID := c.GetInt64("user_id")
	if userID <= 0 {
		resp.Fail(c, resp.CodeUnauthorized, "未登录或登录已失效")
		return nil, false
	}

	var u model.User
	if err := deps.DB.WithContext(ctx).Where("id = ?", userID).First(&u).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			resp.Fail(c, resp.CodeNotFound, "用户不存在，请重新登录")
			return nil, false
		}
		log.Printf("load user failed: user=%d err=%v", userID, err)
		resp.Fail(c, resp.CodeInternal, "查询失败")
		return nil, false
	}
	return &u, true
}

// checkImageContent 读文件头判断真实 MIME，只放行三种位图格式。
// 读完不限速：头像本身受 2MB 上限约束，只读前 512 字节。
func checkImageContent(f *multipart.FileHeader) error {
	fh, err := f.Open()
	if err != nil {
		return err
	}
	defer fh.Close()

	head := make([]byte, 512)
	n, err := io.ReadFull(fh, head)
	if err != nil && !errors.Is(err, io.ErrUnexpectedEOF) && !errors.Is(err, io.EOF) {
		return err
	}
	switch http.DetectContentType(head[:n]) {
	case "image/jpeg", "image/png", "image/webp":
		return nil
	default:
		return errors.New("unsupported image type")
	}
}

// isSafeAvatar 头像必须是本站 avatars 目录下的路径，且不含 ..
func isSafeAvatar(p string) bool {
	if !strings.HasPrefix(p, avatarPrefix) {
		return false
	}
	if strings.Contains(p, "..") || strings.ContainsAny(p, "\\") {
		return false
	}
	// 只允许一层文件名，不接受子目录
	name := strings.TrimPrefix(p, avatarPrefix)
	return name != "" && !strings.Contains(name, "/")
}

// removeAvatarFile 按站内路径删磁盘上的头像文件；清理是尽力而为，失败只记日志、不影响主流程
func removeAvatarFile(rel string) {
	if !isSafeAvatar(rel) {
		return
	}
	name := strings.TrimPrefix(rel, avatarPrefix)
	if err := os.Remove(upload.AvatarPath(name)); err != nil && !os.IsNotExist(err) {
		log.Printf("remove old avatar failed: %s err=%v", rel, err)
	}
}

// trimRunes 按字符（而非字节）截断
func trimRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}
