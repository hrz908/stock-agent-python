# DESIGN.md — 视觉规格（shadcn 风 / 股票工具类小程序）

> 本项目唯一视觉真相源。配套执行文件：`styles/token.wxss`（变量与工具类）、
> `.codebuddy/rules/ui-restyle.md`（执行军规）。
> 争议以本文件为准；本文件未覆盖的，按 shadcn 默认审美判断并回填到这里。

## 色板（亮色）

| Token | 值 | 用途 |
|---|---|---|
| `--bg` | #ffffff | 页面底色 |
| `--surface` | #ffffff | 卡片背景 |
| `--surface-2` | #f4f4f5 | 次级背景（输入框、标签底、进度槽）zinc-100 |
| `--line` | #e4e4e7 | 发丝边框 / 分割线 zinc-200 |
| `--ink` | #09090b | 主文字 + 主按钮底 zinc-950 |
| `--ink-2` | #71717a | 次文字 zinc-500 |
| `--ink-3` | #a1a1aa | 弱文字（字段说明 / 免责声明）zinc-400 |
| `--primary-fg` | #fafafa | 主按钮文字 |
| `--ring` | `--ink` | 焦点描边 |
| `--up` | #dc2626 | 涨（red-600，A股红涨） |
| `--down` | #16a34a | 跌（green-600） |
| `--flat` | #71717a | 平，复用次文字色 |

**强调色收口**：全站只允许一个强调色 `--accent: #2559EB`，且仅用于
**链接 / 焦点**（`md-link`、`nickname-link`、`src-link`、`evt-src`、弹窗确认文字）。
**选中态 / 激活态一律用 shadcn primary**：近黑底（`--ink`）+ 白字（`--primary-fg`），
覆盖 chips（`base-tabs` 与 `kline-card`）、发送键点亮、开关打开时的图标容器、
用户消息气泡。蓝色不做大面积底色、不做标签底、不做选中底。

**状态色只按语义用**：需关注=琥珀（`--warn` 系，标签与时间轴圆点统一）、
危险=`--danger` 系；常规事件圆点用中性灰（`--ink-3`），不占强调蓝。
图表数据系列色（`--accent-deep` 条形、均线三色）属可视化标识，不受上述收口约束。

## 深色模式：不启用（终局）

**2026-10-22 晚用户最终决策：只保留浅色，「太暗了」。** 当天曾短暂整站切到
shadcn dark 并截图验收，随后改回浅色，以本文件为准。
原先 `token.wxss` 里的 `page.dark` 变量块已删除，避免死代码误导。
如将来重启深色课题：从 `style/shadcn` 分支历史取回变量，并先解决每个页面
根节点 `theme` class 的注入方式（列表：chat / watchlist / alerts / settings +
分包 kline）—— 不注入 class 变量不会生效；同时 `app.json` 窗口底色与
canvas 侧 5 个 JS 文件的配色都要同步，只改一半会出现「页面浅、图表深」。

## 字号

**已向 shadcn 层级收敛（2026-10-22 决策）**：原 8 档（64/56/44/40/32/28/22/20 rpx）
统一为 4 个层级值，变量名保留、同档互为语义别名：

| 层级 | 值 | 变量 |
|---|---|---|
| hero | 40rpx | `--fs-price`（行情主价）、`--fs-hero`（总浮动金额/计数/阈值） |
| title | 36rpx | `--fs-value`（次级主数字）、`--fs-brand`、`--fs-title`（卡片/页面标题） |
| body | 30rpx | `--fs-body` |
| caption | 24rpx | `--fs-aux`、`--fs-min`（弱辅助已并入 caption） |

行高：正文 1.6，标题 1.4。数字一律 `.num`（`font-variant-numeric: tabular-nums`）。
新页面只允许引用这 4 个层级，不允许自造第五档。

## 间距刻度（rpx）

只允许这五档：**8 / 16 / 24 / 32 / 48** → `--sp-1` … `--sp-5`。

语义别名：`--pad-card`（卡片内边距）= `--sp-3`、`--pad-page`（页面左右留白）= `--sp-4`、
`--gap-grid`（栅格间距）= `--sp-2`。
遗留的 `--sp-6`(96rpx) 不在五档内，已从 `token.wxss` 移除。

## 形状

- 卡片 / 按钮 / 输入框圆角 **16rpx**（`--r-card`）；标签 / 药丸 **8rpx**（`--r-tag`）
- 边框统一 `--hairline`（2rpx solid `--line`）
- **卡片默认无阴影**（`--shadow-card: none`）；层次靠发丝边框 + 留白表达
- 仅浮层（弹窗 / toast / 抽屉）允许 `--shadow-pop`

## 组件范式

- **行情列表项**：左名称 + 代码两行 / 右现价 + 涨跌幅两行；行高 **112rpx**；
  底部发丝分割线；整列表一张卡，行不单独包卡
- **指标标签**：`--surface-2` 底、22rpx 字、圆角 8、状态色仅作用于文字
- **按钮**：主按钮近黑底白字；次按钮白底描边；高度 ≥88rpx
- **空状态**：线性图标 96rpx 灰 + 两行说明，**禁止 emoji**
- **导航**：88rpx 高 + `env(safe-area-inset-top)`；标题视口绝对居中
- **tabbar**：本项目 `app.json` 未配置 tabBar（页面切换不走 tabbar），
  原手册阶段 4 的 tabbar 改色条目对本项目**不适用**

## 图标

- **统一用 antd-mini 的 `ant-icon`**：用到图标的页面/组件在 json 里声明
  `"ant-icon": "antd-mini/Icon/index"`，WXML 直接写 `<ant-icon type="..." />`。
  自研的 `components/base/icon` 已于 2026-10-23 全量替换后删除。
- **微信里只能用 `antd-mini/Icon/index`**：`antd-mini/es/Icon/index` 是支付宝产物
  （`.axml`/`.acss`），微信编译不了。微信版源码在包的 `compiled/wechat/src`，
  由开发者工具「构建 npm」按 `package.json` 的 `miniprogram` 字段产出到
  `miniprogram_npm/antd-mini/`；改完依赖必须重新构建 npm。
- **尺寸与颜色只能写在 style 上**：组件 v3 没有 `size`/`color` 属性，
  统一 `style="font-size:44rpx;color:var(--ink);"`（rpx 与 CSS 变量都生效）。
- **可用图标**：`miniprogram_npm/antd-mini/Icon/index.wxss` 里的 `.ant-icon-*`，共 152 个。
  官方没有汉堡 / 键盘 / 暂停三枚，现有替代（列表 / 编辑 / 实心麦克风）见各页面注释。
- **字体**：官方组件把 TTF 指向支付宝 CDN，网络字体在 iOS 上不可靠（加载失败整屏空白），
  因此同一份 TTF 已 base64 内联进 `styles/antd-icon.wxss` 并覆盖 `.ant-icon` 的
  font-family；升级 antd-mini 后若 codepoint 有变，需要重新导出字体。

## 导航与返回（子页面退回主页）

- **主页**：`pages/chat/chat`（唯一根页，含抽屉导航）。
- **页面切换方式**：子页面一律由主页 `wx.navigateTo` 进入（抽屉项 / 行情卡 /
  自选股行点击），**不**走 tabBar、不 `redirectTo`，故每个子页都有「上一页」可出栈。
- **返回入口（统一交互）**：所有子页顶部用 `base-navbar` 的 `left-arrow`
  + `bind:back`，点击触发本页 `goBack`（kline 为 `onBack`）出栈返回。
- **返回逻辑（统一写法，四处一致）**：

  ```js
  wx.navigateBack({ fail: () => wx.reLaunch({ url: '/pages/chat/chat' }) });
  ```

  优先 `navigateBack` 出栈回上一页；若栈为空（被单独拉起 / 栈被清）则
  `reLaunch` 兜底回主页。**禁止** `switchTab` 兜底——`chat` 不是 tabBar 页，
  `switchTab` 必然失败，会让兜底也失效。

- **各子页返回路径**：

  | 子页 | 标题 | 进入来源 | 返回目标 |
  |---|---|---|---|
  | `pages/watchlist/watchlist` | 自选股 | 主页抽屉 | 主页（chat） |
  | `pages/alerts/alerts` | 提醒 | 主页抽屉 | 主页（chat） |
  | `pages/settings/settings` | 我的 | 主页抽屉 | 主页（chat） |
  | `pages/kline/index`（分包） | 股票名 / 卡片画廊 | 主页行情卡 **或** 自选股行 | 上一页（chat 或 watchlist）；栈空时兜底回主页 |

  kline 为二级子页，`navigateBack` 回其上一页符合层级直觉；其 `reLaunch`
  兜底保证极端情况下仍能回到主页，不会出现卡死 / 空白页。

## 排版八条（结构与居中专章）

1. 垂直居中唯一写法：flex + `align-items:center`。禁止用 line-height 撑高居中
   （导航标题不居中基本都病源于此）。
2. 水平居中唯一写法：flex + `justify-content:center`，纯文本用 `text-align:center`。
   禁止 `margin:auto` 猜宽度。
3. 导航栏标准：height 88rpx + `padding-top: env(safe-area-inset-top)`；
   title 视口绝对居中（有返回按钮时 `position:absolute` + `left:50%` +
   `translateX(-50%)`，禁止排布居中导致标题跳动）。
4. 文字防溢出三件套：`overflow:hidden` + `text-overflow:ellipsis` +
   `white-space:nowrap`；多行用 `-webkit-line-clamp`。名称 / 标题字段必须带三件套。
5. 数字右对齐列：价格 / 涨跌幅 / 市值统一定宽 + `text-align:right` + 等宽数字；
   名称列 `flex:1` + `min-width:0` 左对齐；数字必须竖向成线。
6. 间距不重叠：相邻元素垂直间距取 max 不取 sum；容器统一 padding，
   子项间用 gap 而非互相 margin。
7. 点击热区 ≥ 88rpx × 88rpx，小图标用 padding 撑热区。
8. 基线节奏：分割线 / 边框统一 `--hairline`；同列表行高统一
   （行情 112rpx，普通 96rpx），禁止逐行自定。

## 禁止清单

- wxss 硬编码 hex / rgba / 非五档间距（必须 `var(--*)`）
- 渐变背景、重阴影、圆角 > 16rpx、彩色分割线
- emoji 当图标；同屏超过 2 种强调色（涨跌色按语义不计）
- 给卡片单独加 `box-shadow`（除非该元素是浮层并显式引用 `--shadow-pop`）

## 项目专属约束（原手册未覆盖，务必遵守）

1. **canvas 双轨**：uCharts（kline 页、kline-card、sparkline）不吃 CSS 变量，
   配色在各自 `.js` 里另写一份。改本文件色值时必须同步那批 JS 硬编码，
   否则「页面换肤、图表没换」。
2. **组件样式隔离**：`components/**` 默认隔离，`styles/token.wxss` 里的
   **自定义属性能透传**、**工具类 class 不透传**。给组件内部节点用工具类，
   需在组件自身 wxss 内复写，或组件 json 设 `styleIsolation`（属结构调整，须走军规第 7 条上报）。
3. **WXSS 是 CSS 受限子集**：禁 `*` 通配符、禁 `:hover/:active`（按压走 `hover-class`）、
   禁 `@media` / `rem` / `vh` / `vw`、单位统一 rpx（安全区与胶囊对齐处除外）。
4. **涨跌语义类**：涨跌平一律用 `.price-up` / `.price-down` / `.price-flat`，
   不要再按 `--up` / `--down` 逐处写 color。
