// Package upload 管理上传目录：main 注入一次，router 用同一个变量挂静态路由。
package upload

import (
	"os"
	"path/filepath"
)

// Dir 上传根目录，默认 ./uploads；静态路由前缀固定为 /uploads
var Dir = "./uploads"

// Init 注入目录并建好子目录
func Init(dir string) error {
	if dir != "" {
		Dir = dir
	}
	return os.MkdirAll(filepath.Join(Dir, "avatars"), 0o755)
}

func AvatarPath(name string) string {
	return filepath.Join(Dir, "avatars", name)
}
