# stock-agent-miniapp/（微信小程序）

原生小程序，**无第三方 UI 库**：navbar / tabs / empty / popup / toast / icon 与 8 张金融卡全部自研。设计 token 集中在 `styles/token.wxss`。

## 页面要点
- `pages/chat`：自绘输入栏胶囊 + 语音转文字（**微信同声传译插件 WechatSI**，`services/asr.js` 薄封装，app.json 需声明 plugins）；K 线卡片限 10 行；流式滚动去抖；**发送看门狗 45s**（无 token 则复位 sending 并提示，避免「点了发送没反应」）；输入栏手势用 touchstart 标记而非 catch（catch 会让子元素点击全失效）
- `pages/settings`：昵称输入焦点铁律 —— `nicknameFocus` 必须镜像真实焦点，收键盘统一用 `wx.hideKeyboard()`；`type=nickname` 仅在「取微信昵称」期间存在；输入框加 `hold-keyboard`

## 构建
- `npm install`；微信开发者工具「构建 npm」（`miniprogram_npm/` 是构建产物、不入库，换机器必须重走）
- 依赖：marked ^4（markdown 渲染）、uCharts（K 线 canvas）
- 历史包袱已清除：TDesign 移除后 `scripts/fix-marked.js` 与 `npm run fix:npm` 一并删除

## 视觉
- 浅色主题：页面底 `--bg` #F5F6F8，卡片 `--surface` 白底 + 细描边 + 轻投影（`--shadow-card`）
- 主行动色 `--accent` #2559EB（唯一强调色）；次级蓝 `--accent-deep/--accent-soft`
- 红涨绿跌：`--up` #F04A4A / `--down` #1AA97C；`--up` 只表示行情涨跌，删除/退出/校验错误用 `--danger`

## 验证
- 微信开发者工具**可 CLI 调用**（2026-10-25 复核）：`& "C:\Program Files (x86)\Tencent\微信web开发者工具\wechatide.cmd" -c CodeBuddy <tool> --project <项目绝对路径>`，已登录，`skill-call` 无需服务端口与 token；可 `open_project_window` / `simulator_open_page` / `simulator_screenshot` / `get_simulator_console` 做真机级验证
- 改输入栏 / 弹窗 / 手势 / WS 消息渲染前必读 `.codebuddy/rules/miniapp-gotchas.md`

## 相关
`mem:core`、`mem:conventions`