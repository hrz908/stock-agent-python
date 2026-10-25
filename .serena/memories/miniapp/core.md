# stock-agent-miniapp/（微信小程序）

原生小程序，**无第三方 UI 库**：navbar / tabs / empty / popup / toast / icon 与 8 张金融卡全部自研。设计 token 集中在 `styles/token.wxss`，视觉规则以 `docs/DESIGN.md` 为唯一真相源。

## 页面要点
- `pages/chat`：自绘输入栏胶囊 + 语音转文字（**微信同声传译插件 WechatSI**，`services/asr.js` 薄封装，app.json 需声明 plugins）；K 线卡片限 10 行；流式滚动去抖；**发送看门狗 45s**（无 token 则复位 sending 并提示，避免「点了发送没反应」）；输入栏手势用 touchstart 标记而非 catch（catch 会让子元素点击全失效）；首屏问候下方是三个可点建议问题（`showSuggestions`，发出第一条即撤掉）
- `pages/settings`：昵称输入焦点铁律 —— `nicknameFocus` 必须镜像真实焦点，收键盘统一用 `wx.hideKeyboard()`；`type=nickname` 仅在「取微信昵称」期间存在；输入框加 `hold-keyboard`

## 构建
- `npm install`；微信开发者工具「构建 npm」（`miniprogram_npm/` 是构建产物、不入库，换机器必须重走）
- 依赖：marked ^4（markdown 渲染）、uCharts（K 线 canvas）
- 历史包袱已清除：TDesign 移除后 `scripts/fix-marked.js` 与 `npm run fix:npm` 一并删除

## 视觉
- 纯白底（`--bg` = `--surface` = #ffffff）：层次只靠留白与 `--surface-2` #f4f4f5 浅底块表达，**金融卡本身无边框、无底色、无内边距**，不叠框
- 对话页：AI 回复（文本/卡片/状态）统一挂左侧 2rpx 页边线；用户提问是浅底小块，不用近黑实底
- 文字三档 zinc：`--ink` #09090b / `--ink-2` #52525b / `--ink-3` #6b6b73，白底与浅底块上都过 4.5:1
- 字号五档：display 64（每屏只允许一个）/ hero 40 / title 36 / body 30 / caption 24
- 红涨绿跌：`--up` #dc2626 / `--down` #15803d；`--up` 只表示行情涨跌，删除/退出/校验错误一律 `--danger`
- canvas / SVG 侧（`utils/color.js`、`kline-card.js`）另存一份色值，改 token 必须同步

## 验证
- 微信开发者工具**服务端口已关闭**，不能 CLI 自动化验证，手动编译查看
- 改输入栏 / 弹窗 / 手势 / WS 消息渲染前必读 `.codebuddy/rules/miniapp-gotchas.md`

## 相关
`mem:core`、`mem:conventions`