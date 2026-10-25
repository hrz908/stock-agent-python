# stock-agent-miniapp/（微信小程序）

原生小程序，**零 UI 库、零图标库**：navbar / tabs(已删) / empty / popup / toast / icon 与 8 张金融卡全部自研。设计 token 集中在 `styles/token.wxss`（浅色 zinc 色阶，`--up #dc2626` / `--down #16a34a` / `--accent #2559EB`）。

## 页面要点
- `pages/chat`：自绘输入栏胶囊 + 语音转文字（**微信同声传译插件 WechatSI**，`services/asr.js`，app.json 需声明 plugins）；流式渲染走 `services/agent.js` 单例（`enableChunked` + 自写 UTF-8 增量解码 + `\n\n` 切帧，事件带 `__rid` 供多页实例过滤）；markdown 版式重解析有 120ms 节流；发送看门狗是**空闲超时**（每个 token 重置 45s）；发送前必须 `ensureLogin`（SSE 不走 request 的 401 重登）。
- `pages/settings`：昵称输入焦点铁律 —— `nicknameFocus` 必须镜像真实焦点，收键盘统一用 `wx.hideKeyboard()`；`type=nickname` 仅在「取微信昵称」期间存在；注销账号需先逐条删会话再删账号。
- `pages/watchlist`：进页只调 `fetchList()`（读缓存），`refresh()` 仅在用户点刷新时调（会拉第三方行情 + 广播提醒检查）。

## 依赖与体积
- 只需 `marked ^4`（markdown lex）；`npm install` + 开发者工具「构建 npm」生成 `miniprogram_npm/`（不入库）。
- 图标用 `components/base/icon`（字体码点表 + `styles/antd-icon.wxss` 的 base64 TTF），**不要再引入 antd-mini**（曾为 10 个图标背 2.5MB 依赖链）。
- `assets/logo-bull.png` 144×144 / 60KB，别再放大图。

## 验证
- 微信开发者工具可 CLI 调用：`& "C:\Program Files (x86)\Tencent\微信web开发者工具\wechatide.cmd" -c CodeBuddy <tool> --project <项目绝对路径>`，已登录；可 `open_project_window` / `simulator_open_page` / `simulator_screenshot` / `get_simulator_console`（参数 `--command "grep -in -e error"`）/ `build_npm`。
- `npm test` 跑两套：`tests/newchat`（对话页交互，自建 vm 桩，卡片模块用真实实现）+ `tests/unit`（format / markdown / SSE 分帧 / cards 映射）。
- `npm run lint`：ESLint 10 扁平配置（只开 no-undef 一类硬错误，风格规则一概不开）；`eslint.config.js` 已在 packOptions.ignore 里，不进包。

## 相关
`mem:core`、`mem:conventions`