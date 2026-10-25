# 代码约定

## Go（server/）
- 分层：`internal/` 下除 `module/` 全是共享基础设施；`module/xxx/` 每模块自带 `model.go` + `handler.go` + `route.go`（`func Register(g *gin.RouterGroup)`）
- 加新页面后端三步：建 `module/<名>/` → `router/router.go` 里 Register → `main.go` 的 models 汇总加上（否则不建表）
- **模块之间禁止互相 import**；跨模块联动走 `internal/hook` 事件订阅（现用法：watchlist 刷新完广播「行情已更新」，alert 订阅做提醒检查）。确需按表清理时在自身包里声明「只有表名」的浅模型（见 `module/user` 的 purgeWatchlist / purgeAlertRule）
- 金额用 `shopspring/decimal`；响应统一走 `internal/resp`；限流/鉴权/CORS 在 `internal/middleware`
- 迁移：`migrations/` 只在**数据卷为空时**执行一次（挂到 docker-entrypoint-initdb.d），改表必须新增迁移文件，禁止改已有的
- 配置：数值型环境变量解析失败**直接终止启动**（不静默回退）；`ENV=prod` 时 JWT_SECRET 或微信凭据仍是默认值则拒绝启动

## 行尾陷阱（Windows 必知）
- 根目录 `.gitattributes` 已写 `*.go text eol=lf`，仓库内存 LF；本机 `core.autocrlf=true` 时检出仍是 CRLF，gofmt 会误报未格式化。校验用 `gofmt -l <改动的包>`，不要 `gofmt -w .`（会把整仓行尾改写）
- golangci-lint 默认 `max-same-issues: 3`，看全量要加 `--max-same-issues=0`

## Python agent
- 意图路由：`mcp`（要实时/结构化数据，走工具）/`rag`（查知识库）/`chat`/`clarify`
- RAG 不得编造；未命中要如实说明；无数据且命中行情关键词时走固定兜底文案
- 行情/ K 线每轮必须重新取数，**禁止沿用历史消息里的数字**（等于给过期答案）
- 工具子图轮次上限 3；无数据不得自由发挥
- 容器里访问宿主机 Go 服务必须用 `host.docker.internal`（不是 127.0.0.1）

## 小程序
- 颜色一律用 `styles/token.wxss` 的变量（主强调色 `--accent` #2559EB），**禁用 TDesign 默认主色 #0052D9**；红涨绿跌：`--up` 只表示行情涨跌，删除/退出/校验错误用 `--danger`
- 模块统一 ESM（`import` / `export`），不再混用 `require` / `module.exports`
- 图标一律 `<base-icon type="..." name>`（`components/base/icon`），不要再引 antd-mini
- 昵称输入：`nicknameFocus` 必须镜像真实焦点（收键盘用 `wx.hideKeyboard()`）；`type=nickname` 只在「取微信昵称」期间存在
- 验证走开发者工具 CLI（见 `mem:miniapp/core`），不再要求手工编译

## 提交
- Conventional Commits + 中文正文，scope 用中文模块名：`feat(小程序): `、`fix(server): `、`chore: `