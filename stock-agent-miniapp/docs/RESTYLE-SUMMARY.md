# RESTYLE-SUMMARY.md — shadcn 重构总结

> 分支 `style/shadcn`（基线 8360881）｜ 2026-10-22 完成阶段 0~4
> 视觉规格唯一真相源：`DESIGN.md`；执行军规：`.codebuddy/rules/ui-restyle.md`

## 一、五阶段执行记录

| 阶段 | 内容 | 产物 |
|---|---|---|
| 0 基建 | token 就地升级为 shadcn zinc 色板 + 排版工具类；军规落盘；JS/配置侧配色同步 | `DESIGN.md`、`styles/token.wxss`、`app.wxss`、`.codebuddy/rules/ui-restyle.md` |
| 1A 巡检 | 5 页 + 15 组件对照排版八条，产出 91 条问题（P0 去重后 2 个根因）+ 4 项渲染缺陷 | `LAYOUT-ISSUES.md` |
| 1B 修复 | 修 88/91 条；3 条入 BLOCKED | 见 `LAYOUT-ISSUES.md` 第〇节 |
| 2 试点 | settings 页配色改造：底部按钮并入 shadcn 主按钮范式 | `pages/settings/settings.wxss` |
| 3 批量 | watchlist/alerts 主按钮蓝→近黑；navbar px→rpx；标签 padding、圆角、胶囊收口 token | 各页 wxss + `token.wxss` |
| 4 收尾 | 全局扫遗留清零；本文档 | 本文件 |

## 二、视觉变化总览

- 页面底 `#F5F6F8` 浅灰 → **纯白 #ffffff**；卡片去投影（`--shadow-card: none`），层次靠发丝边框
- 主按钮：主行动蓝 `#2559EB` → **近黑 #09090b + 白字**；蓝色降级为链接/选中态/焦点专用
- 涨跌色 `#F04A4A/#1AA97C` → `#dc2626/#16a34a`（红涨绿跌语义不变）
- 圆角 32rpx → 16rpx（标签 12→8rpx）；新增 `--radius-md` 胶囊档
- 边框统一 `--hairline`（2rpx），53 处手写 1px/1rpx 收口
- navbar 标题从「贴左」改为**视口绝对居中**（惠及 5 页）
- 8 张金融卡主标题补截断；**卡片内 `.num` 等宽数字复写**（组件隔离导致此前 33 处失效）
- SVG 图标 / K 线 / sparkline 配色同步到新 token（此前与页面脱色）

## 三、截图证据

`.restyle-shots/`（gitignore，本地产物）：

| 文件 | 内容 |
|---|---|
| 01-chat-after-sync / 02-watchlist-after-sync | 阶段 0 配色同步后 |
| 05 / 10 / 11 / 12 / 13-*-1b | 阶段 1B 排版修复后（chat/watchlist/alerts/settings/kline） |
| 20 / 21 / 22 / 23 / 24-*-phase3 | 阶段 2~3 后终态（主按钮近黑、navbar 居中） |

## 四、最终自检（2026-10-22）

- 项目源码 24 个 wxss：**硬编码 hex 清零**（仅存于 `token.wxss` 定义处）
- 168 个 wxss（含 node_modules 产物）括号平衡；24 个项目 wxss 注释闭合、无游离声明
- 非 token 间距/圆角仅剩 6 处，均已加「例外」注释并说明原因
- 旧 token 33 个变量：删除 0 个（无失效引用）
- 五页截图无编译错误条；`mp_screenshot` 通道（weapp-dev MCP，connect 9420）验证可用

## 五、BLOCKED 清单（需改 wxml/js，共 12 项）

见 `RESTYLE-BLOCKED.md`。其中影响最大的四项：

1. `utils/color.js` 等 6 处 JS/配置 —— **已获授权并完成**（阶段 0）
2. `sparkline` 外部类（`spark-class` 未传）—— **已修复**（验收后授权）：quote-card / fundflow-card 两处 wxml 改传 `spark-class=`
3. `navbar.wxml` 内联 `max-width` 彻底版 —— CSS 版已修，待真机确认
4. `settings` 原生 switch 热区 56rpx < 88rpx —— 需包可点容器

## 六、主题：仅浅色（终局）

当天经历三次反复：**浅色专业金融 → shadcn 亮色 → 短暂整站 shadcn dark →
晚 用户以「太暗了」改回浅色**。两轮深色决策均以当晚终局为准，本项目的主题就是
浅色 shadcn，不要再按中间态改。

- `token.wxss` 即浅色值；`page.dark` 变量块已删除，不做主题切换
- `app.json` 与 3 个页面 json 窗口底色 `#ffffff`、`navigationBarTextStyle: black`
- canvas/SVG 侧 5 个 JS 文件（`utils/color.js`、`pages/kline/index.js`、
  `kline-card.js`、`sparkline.js`、`empty.js`）同为浅色值
- 深色专属规则（主按钮白底深字、强调色 blue-400、状态色提亮、投影 0.5 黑）
  随深色批次一并回退，`DESIGN.md` 已无这些条目

## 七、字号层级：已收敛

原 8 档（64/56/44/40/32/28/22/20）收敛为 shadcn 四层：**hero 40 / title 36 /
body 30 / caption 24**。变量名保留（300+ 处引用不动），同档变量互为语义别名
（price≡hero、value≡brand≡title、aux≡min）。body 由 28→30 为放大，无无障碍风险。

## 八、后续建议（按优先级）

1. **真机验收**：navbar 标题居中、88rpx 热区、长股票名截断、新字号下的换行/溢出
   —— 模拟器无法覆盖真机差异
2. BLOCKED 剩余项：`navbar.wxml` 内联 `max-width` 彻底版（CSS 版已生效并通过验收）、
   `settings` 原生 switch 热区（需包可点容器）、`alerts` 输入框单位居中（P2）

## 九、已完成的收尾（2026-10-22 验收后）

- `sparkline` 外部类修复：quote-card / fundflow-card 改传 `spark-class=`（BLOCKED #7）
- TDesign 残留清理：`package.json` 早已无该依赖、全项目零引用（仅注释提及），
  `node_modules/.tdesign-miniprogram-*` npm 缓存已删除；
  `miniprogram_npm/` 实际只含 `@qiun/ucharts` 与 `marked`，主包未被污染，
  「残留包缩小主包」的担心不成立
- 字号收敛（8 档 → shadcn 四层 hero 40 / title 36 / body 30 / caption 24）
- 主题反复：短暂整站深色 → **回浅色（终局，「太暗了」）**，详见第六节
- 已合并 `main`（merge commit `7c6f9ea`），`style/shadcn` 分支保留作历史记录
