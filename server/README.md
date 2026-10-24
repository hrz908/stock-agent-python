# 溜来 · 后端服务（server）

技术栈：**Gin + GORM + PostgreSQL + go-redis/v9 + gorilla/websocket**

**仓库定位**：本目录与小程序工程 `stock-agent-miniapp` **平级**，是一个独立的 Go 后端服务，
不属于任何单个小程序工程。后续其它页面（或其它端）需要后端能力时，一律在**本目录内按模块扩展**，
不要再新建第二个后端工程。

当前已实现「自选股」页面所需接口：添加自选（带成本价 / 持股数量）、查询列表（含浮动盈亏与涨跌排序）、
手动刷新行情（含提醒检查与 WebSocket 推送）、提醒规则 CRUD（条件类型 / 阈值 / 基准周期 / 检查频率，
与「新增提醒」弹窗字段一致）、用户资料与头像。

---

## 一、运行

### 1）起依赖（PostgreSQL + Redis）

```bash
cd server
docker compose up -d     # 首次会自动跑 migrations/ 下的 SQL 建库建表
docker compose ps        # 等两个容器都 healthy 再起服务
```

> `docker-compose.yml` 把 `migrations/` 挂到了 `docker-entrypoint-initdb.d`，
> 只在**数据目录为空时**执行一次。之后改表请加新的迁移文件，别直接改 `001_init.sql`。

### 2）起服务

```bash
go mod tidy          # 首次需要联网拉依赖
go run .
```

冒烟验证：

```bash
curl -s http://127.0.0.1:8080/healthz
# {"code":0,"data":{"status":"ok"},"message":"success"}
```

环境变量（都有默认值，本地可直接起）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ADDR` | `:8080` | 监听地址 |
| `DSN` | `postgres://postgres:postgres@localhost:5432/stock_agent?sslmode=disable` | PostgreSQL |
| `REDIS_ADDR` | `127.0.0.1:6379` | Redis |
| `REDIS_PASSWORD` | 空 | Redis 密码 |
| `REDIS_DB` | `0` | Redis 库 |
| `JWT_SECRET` | `please-change-me` | **上线必须改**（`ENV=prod` 时仍是默认则拒绝启动） |
| `QUOTE_API` | 空 | 自建行情服务基地址；**为空时用内置真实源**（腾讯财经批量接口，见 `quote/source.go`） |
| `WECHAT_APPID` / `WECHAT_SECRET` | 空 | 小程序凭据；两者都配了登录走 code2session，都没配时退化为演示账号（`ENV=prod` 拒绝启动） |
| `UPLOAD_DIR` | `./uploads` | 上传根目录；头像存 `avatars/`，静态路由只暴露 `/uploads/avatars` |
| `ENV` | `dev` | `dev` / `prod`。`prod` 下强制要求 JWT 与微信凭据 |
| `ALLOW_ORIGINS` | `*` | CORS 允许的 Origin，逗号分隔；`*` 表示全部 |
| `ASR_BASE_URL` | `https://api.siliconflow.cn/v1` | 语音转文字上游（OpenAI 兼容的 `/audio/transcriptions`） |
| `ASR_API_KEY` | 回退 `OPENAI_API_KEY` | 上游密钥；两者都为空时 `/api/v1/asr/transcribe` 直接报「未配置」 |
| `ASR_MODEL` | `FunAudioLLM/SenseVoiceSmall` | 上游 ASR 模型 |
| `TENCENT_ASR_APP_ID` / `TENCENT_ASR_SECRET_ID` / `TENCENT_ASR_SECRET_KEY` | 空 | 腾讯云实时语音识别凭据；三项全配才启用 `/api/v1/ws/asr` 流式识别，否则该路由返回 503。小程序语音已改用微信同声传译插件，这两条 ASR 路由目前没有前端调用方 |
| `TENCENT_ASR_ENGINE` | `16k_zh` | 实时识别引擎（电话场景可用 `8k_zh`） |
| `TENCENT_ASR_MAX_CONNS` | `64` | 进程内同时存活的实时识别连接上限 |

数值型配置（`REDIS_DB` 等）解析失败会直接终止启动：写错环境变量时静默回退默认值，
问题会藏到运行期才暴露，不如启动时就报错。

启动时会自动建表（AutoMigrate，只覆盖用户 / 自选 / 提醒）。`migrations/` 里 005 之后
（向量库、A股语料、A股主数据、财务指标）都由 SQL 迁移持有，不进 AutoMigrate；
已有库改表请新增迁移文件并手工执行（initdb 只在空数据卷跑）。

---

## 二、目录结构

```
server/
  main.go                       # 启动、依赖注入、优雅关闭
  internal/
    config/config.go            # 环境变量
    deps/deps.go                # 全局 db / rdb
    db/postgres.go              # GORM 连接 + 自动迁移
    db/redis.go                 # Redis 客户端
    quote/quote.go              # 行情缓存：MGET 批量读、Pipeline 批量写
    quote/source.go             # fetchStockQuote（内置腾讯真实源 / 自建源，可替换）
    resp/resp.go                # 统一响应体 + resp() / JSONBytes 辅助函数
    model/user.go               # 跨模块共享的模型（User：登录与用户资料共用）
    middleware/middleware.go    # Recovery / RequestID / CORS / Auth / AuthAllowQueryToken / RateLimit / AccessLog
    ws/hub.go                   # WebSocket 连接中心 + Push
    hook/hook.go                # 模块间事件（行情已更新）
    domain/stockcode/code.go    # 股票代码归一化 / 展示格式
    util/amount.go              # 金额与涨跌幅格式化
    module/                     # 业务模块：一个页面一个包，自带 model/handler/route
      auth/
        handler.go              # 登录：code → uid → JWT
        route.go
      watchlist/
        model.go                # UserWatchlist
        handler.go              # 添加 / 列表 / 刷新
        route.go
      alert/
        model.go                # AlertRule + 条件枚举
        handler.go              # 提醒 CRUD + checkAlerts
        route.go
      stock/
        handler.go              # 搜索 / 行情快照 / 主数据资料
        master.go               # 主数据只读查询（stock_basic、v_stock_profile）
        route.go
    router/router.go            # 只负责挂载各模块
  migrations/001_init.sql       # 建表 SQL
  migrations/002_users.sql      # 用户表
  migrations/003_alert_rule.sql # alert_rule 升级：对齐「新增提醒」弹窗字段
  migrations/004_alert_unique.sql # 提醒规则唯一索引（去重后建）
  migrations/008_stock_master.sql # A股主数据：清单/行业/概念 + v_stock_profile
  migrations/009_stock_finance_indicator.sql # 报告期财务宽表（P1，可选启用）
```

**分层约定**：`internal/` 下除 `module/` 外都是与具体页面无关的共享基础设施；
`module/xxx/` 才是业务代码，每个模块自带 `model.go` / `handler.go` / `route.go`。

**新增一个页面的后端，三步**：

1. 新建 `internal/module/<模块名>/`，写 `model.go`（GORM 结构体 + `var Models = []any{...}`）、
   `handler.go`（业务）、`route.go`（`func Register(g *gin.RouterGroup)`）；
2. 在 `router/router.go` 里 import 并调用一次 `<模块名>.Register(authorized)`
   （公开接口则挂到未鉴权的 `api` 组上）；
3. 在 `main.go` 的 `models` 汇总处加上该模块的 `Models`，让建表覆盖到新表。

模块之间**不要互相 import**：跨模块的联动走 `internal/hook` 的事件订阅
（现在的用法：`watchlist` 刷新完广播“行情已更新”，`alert` 订阅后做提醒检查）。

---

## 三、接口总表

| 方法 | 路径 | 作用 | 需登录 |
|---|---|---|---|
| POST | `/api/v1/auth/login` | 登录发 JWT | 否 |
| GET | `/api/v1/stocks/search` | 股票搜索（腾讯 smartbox 优先，本地主数据兜底） | 是 |
| GET | `/api/v1/stocks/profile` | 股票主数据：板块 / 行业 / 概念 / 上市退市 | 是 |
| GET | `/api/v1/stock/quote` | 单只行情快照（30s Redis 缓存） | 是 |
| GET | `/api/v1/watchlist` | 列表（含盈亏、排序） | 是 |
| POST | `/api/v1/watchlist` | 添加 / 更新成本与数量 | 是 |
| POST | `/api/v1/watchlist/refresh` | 手动刷新 + 提醒检查 | 是 |
| DELETE | `/api/v1/watchlist/:stock_code` | 移出自选（软删除） | 是 |
| POST | `/api/v1/alerts` | 新建提醒规则 | 是 |
| GET | `/api/v1/alerts` | 本人提醒列表 | 是 |
| PUT | `/api/v1/alerts/:id` | 修改提醒 | 是 |
| DELETE | `/api/v1/alerts/:id` | 删除提醒 | 是 |
| GET | `/api/v1/ws` | WebSocket 长连接 | 是 |

### 主数据（迁移 008）

`stock_basic` / `stock_industry` / `stock_concept` 三张表 + 视图 `v_stock_profile`，
由 `ingest` 的 `python -m ashare.cli sync-master` 灌数（日更清单、周更行业与概念）。

- `GET /stocks/search`：先打腾讯 smartbox；上游失败或返回空时用 `stock_basic` 兜底
  （代码前缀 + 名称/拼音三元组匹配）。响应头 `X-Search-Source` 标明本次来源
  （`tencent` / `db` / `empty`），排查「搜不到」时先看它。
- `GET /stocks/profile?code=`：读 `v_stock_profile`，Redis 缓存 1 小时；表里没有该股票
  返回 404（多为 `sync-master` 还没跑）。代码可传 `600519` / `SH600519` / `600519.SH`。
- **行情与估值不在这两张表里**：它们是每日变动的快照，走 `/stock/quote` 的实时源 + 30s 缓存。
- 这两张表刻意不进 `AutoMigrate`：结构与索引由 SQL 迁移持有，避免 GORM 与迁移脚本分叉。

### Agent WS 事件契约（`/api/v1/ws/agent`）

网关只做透传，`data` 结构由 Python Agent（`agent/subgraphs/stock.py` 的 `_emit_structured`）约定；
改卡片类型必须同步 `stock-agent-miniapp/pages/chat/chat.js` 的卡片工厂与本文本表。
八张金融卡的数据全部由 agent 的结构化输出（`card` / `table` 事件）提供，前端不再有 mock 分支。

| event | data.type | 载荷 | 前端渲染 |
|---|---|---|---|
| `card` | `quote_card` | `QuoteDetail` 单对象（code/name/price/change/change_percent/open/high/low/volume/amount/turnover/pe/pb/total_mv/free_mv/status/time） | 行情速览卡 |
| `card` | `valuation_card` | 东财估值单行（字段名沿用 daily_basic 口径：pe/pe_ttm/pb/ps_ttm/total_mv；**金额单位万元**；dv_ttm 当前数据源无此字段，恒空） | 估值卡 |
| `card` | `fundflow_card` | 东财资金流行经发射层换算（字段名沿用 moneyflow 契约：buy/sell_sm/md/lg/elg_amount、net_mf_amount；**金额单位万元**，由净额正负拆分而来）；
命中龙虎榜时并入 `lhb`（`[{seat, buy, sell}]`，仅此三个量，**万元**）与 `lhb_date` | 资金流向卡（含龙虎榜区块） |
| `card` | `report_card` | `{indicator, income, cashflow}` 三表合并（新浪报表+财务指标，各为单行 dict，可缺表；**金额单位元**） | 财报速览卡 |
| `card` | `compare_card` | `{items, ts_code, peers_source}`；items 为 `[{name, price, changePct, pe, pb, totalMv}]`（**当日快照，元口径**，最多本股 + 2 家同行）；`peers_source` = `explicit`（模型指定）/ `industry`（按行业自动补）/ `none`（只比本股） | 同业对比卡 |
| `card` | `research_card` | 东财研报单行（org/rating/report_type/publish_date/summary/source_name/source_url/industry/eps_forecast/pe_forecast；**数据源无目标价**，targetPrice 恒空） | 研报观点卡 |
| `card` | `event_card` | `{events, range, note}`；events 为 `[{type, date, title, desc, important, source_url, source_name}]`（由 `get_stock_news` 当日资讯确定性归纳，不做语义推断） | 公司事件卡 |
| `table` | `symbol_list` | 股票搜索候选行数组 | 搜索候选列表 |
| `table` | `kline_table` | `{rows: [{date, open, high, low, close, volume}], code?, name?}`（升序，最多 15 个交易日；`trade_date→date`、`vol→volume` 已映射；本轮查过行情时补 `code`/`name` 供卡片标题用） | K 线卡 |

说明：

1. **一次提问，一种卡片只发一张**（防刷屏）。估值 / 资金流的数据源按「天」返回一整段，
   只取最新一天发一张卡（按天发会把同一只股票的连续交易日并排铺三张）；研报取最新一篇；
   资讯整轮合成一张事件卡。多条目之间的差异由正文转述，卡片只做速览；
2. `report_card` 来自组合工具 `get_financial_report`（并发拉新浪财务指标 + 利润表 + 现金流量表），
   任一子表取不到时对应字段为 `null`，前端渲染 `--`，不影响其它字段；
3. `kline_table` 由 `get_history_daily` 发射（2026-10-23 复活；Go `/stock/kline` 仍保持下线），
   产品约定 K 线走势只取 15 个交易日，工具侧窗口收死（days 上限 15）；
4. 龙虎榜由 `get_top_list`（东财龙虎榜详情）提供，只取席位/买入/卖出三个量；
   近期未上榜时优雅降级（fundflow 卡不带 lhb 区块）。北向/南向/融资融券工具保持下线；
5. `compare_card` 由 `get_peer_compare` 提供，五个维度全部来自 Go 网关当日行情快照
   （腾讯源）；历史分位、行业均值、一致预期等字段仍**没有数据源**，
   卡片按缺失降级，不编造。`peers` 留空时会按东财行业自动补两家（`peers_source=industry`），
   只是兜底；要挑规模相近的同行，模型应先调 `get_industry_peers` 再显式传 `peers`。
6. **数据源（2026-10-23 起）**：卡片数据 = akshare（东财/新浪，免 token）+ baostock（历史日 K，免 token），
   原 Tushare 通道已删除（实测 token 对 moneyflow/top_list 等接口无权限 40203）；
   quote/search/compare 仍走 Go 网关腾讯快照。
7. `research_card` 由 `get_research_reports`（东财个股研报）提供，取最新一篇（多家机构的
   观点差异由正文转述）。东财研报不提供目标价，`targetPrice` / `upside` / `forecastDir` 恒空，
   卡片降级显示 `--`，工具层不编造。
8. `event_card` 由 `get_stock_news`（东财个股资讯，只保留当日）整轮合成一张卡（不再一条一卡）。
   事件类型按标题关键词确定性归纳（解禁 / 增减持 / 分红 / 回购 / 停复牌 / 诉讼 / 订单 / 财报），
   归纳不出归「资讯」；`important` 只标解禁 / 停复牌 / 诉讼三类。

---

## 四、业务规则与降级策略

### 添加自选股

1. 先拿行情再写库——**行情接口失败直接返回 502，不写数据库**，避免出现「有记录但没名称」的脏数据；
2. 写库用 Upsert（`OnConflict`），基于 `(user_id, stock_code)` 唯一索引；
3. 重复添加时**只更新 `cost_price` / `quantity`（传了才更新，没传保持原值）**，不覆盖股票名称；
4. 写库成功后把行情快照写入 Redis（`stock:{code}:latest`，30 秒过期），**Redis 失败不影响接口成功返回**，只记日志。

### 查询列表

1. 基础信息来自 PostgreSQL，实时价从 Redis **一次 MGET 批量读取**（50 只自选股从 50 次往返降为 1 次）；
2. 盈亏在后端算好：
   - `profit_amount = (最新价 - 成本价) × 数量`
   - `profit_rate = (最新价 - 成本价) × 数量 ÷ (成本价 × 数量) × 100`
   - 成本价为 0（用户没填）时盈亏比例记 0，避免除零；
3. 同时返回总计 `total_profit` / `total_profit_rate` 与 `up_count` / `down_count`；
4. 排序：`filter=up` 按涨跌幅降序、`filter=down` 升序，`filter=all` 默认按加入时间倒序（也可显式传 `order=asc|desc`）；
5. `pending: true` 表示刚加入还没有行情（前端渲染 `--`），它不参与涨跌榜与统计。

### 手动刷新

1. 去重 → 按 20 只一批切分 → 批与批之间并发（上限 5），
   内置源支持一次查多只，50 只自选股只要 3 次 HTTP 往返；
2. 成功 → 写 Redis 并用新数据返回；
3. 失败 → **降级读旧缓存**，标记 `stale: true`；旧缓存也没有 → 价格 0、`pending: true`、`stale: true`；
4. 行情更新完成后才广播「行情已更新」事件，alert 模块收到后做提醒检查，避免拿旧价误判；
5. **本接口不写 `user_watchlist` 表**（列表没变化，只更新缓存）；
6. 返回结构与 `GET /watchlist` 一致（`{list, total_profit, ...}`），前端可直接整段替换；
7. 不使用 cron，所有行情拉取由前端主动触发。

### 移出自选

- 走**软删除**：只把 `enabled` 置为 false，不物理删行，
  保留成本价等建仓信息，重新添加时自动恢复（Upsert 会带上 `enabled`）；
- 更新必须带 `user_id` 过滤，防止改到别人的记录；
- 查不到统一 404，不区分「不存在」与「非本人」。

### 提醒规则

- 条件枚举与小程序「新增提醒」弹窗一一对应：`price_up`（最新价 ≥ 阈值）、`price_down`（最新价 ≤ 阈值）、
  `pct_up`（涨跌幅 ≥ 阈值%）、`pct_down`（涨跌幅 ≤ −阈值%）、`volume`（成交量 ≥ 阈值 × N 日均量）；
- 阈值取值范围随条件类型：价格 > 0，涨跌幅 0~100，成交量倍数 0.1~100；
- `volume_base`（5 / 10 / 20 日均量）只有成交量异动需要，其它条件后端强制归 0；
- `check_frequency`：`realtime`（盘中即时，每次行情刷新都判）、`close`（日线收盘确认，只在 15:00 后判）；
- 同一用户、同一股票、同一条件类型只允许一条**启用**规则，重复创建返回 409；
- `PUT` / `DELETE` 一律用 `id + user_id` 双条件定位，查不到统一 404，不区分「不存在」与「非本人」，防止枚举探测；
- 改了判定条件（条件类型 / 阈值 / 基准周期 / 频率）会清空 `last_triggered_at`，让规则能重新触发；
- 删除或停用时 `DEL alert:lock:{user_id}:{stock_code}:{condition_type}`，避免残留防抖锁挡住后续推送；
- 推送防重复：`SetNX alert:lock:{user_id}:{stock_code}:{condition_type}`，TTL 60 秒，写不进去就跳过；
- **成交量异动暂不自动判触发**：行情快照里只有最新价与涨跌幅，没有成交量与 N 日均量。
  规则可正常存取，等行情源补上成交量字段后，在 `isHit` 的 `CondVolume` 分支补比较即可。

### 金额精度

所有价格、成本、盈亏的计算一律使用 `github.com/shopspring/decimal`，**禁止用 `float64` 做加减乘除**（否则会出现 `3682.4000000000005` 这类误差）。对外输出的金额统一 `StringFixed(2)`，成本价 `StringFixed(4)`。

### 行情源

**默认开箱即用**：没配 `QUOTE_API` 时走内置的真实行情源——腾讯财经批量接口
`https://qt.gtimg.cn/q=sh600519,sz000001`。它返回 GBK 文本（用 `golang.org/x/text` 转 UTF-8），
字段以 `~` 分隔，我们取下标 `1`（名称）、`3`（最新价）、`32`（涨跌幅）。

**换成自建源**：设 `QUOTE_API` 即可，协议约定：

```http
GET {QUOTE_API}?code=SH600519
→ {"code":"SH600519","name":"贵州茅台","price":"1329.00","change_percent":"1.22"}
```

数值一律用字符串传递，避免中间环节走 float64 丢失精度。

**批量拉取**：一次刷新按 20 只一批并发请求（批间并发上限 5），
50 只自选股从「50 次逐只请求」降到 3 次 HTTP；响应体读取限 1MB，
避免上游异常时把超大内容读进内存。缓存读写同理：MGET 按 100 个 key 分片。

**降级**：某批拉取失败不影响其它批，失败的 code 降级读旧缓存；
读不到就保持 `--` 无行情状态，不写脏数据。

**拉取策略**：去重 → 按 20 只一批切分 → 批与批之间并发（上限 5）。
内置源支持一次查多只，所以 50 只自选股只要 3 次 HTTP 往返，而不是 50 次。

**异常处理**：

- 单次请求超时 3 秒，第三方卡住不会拖死刷新接口；
- HTTP 客户端全局复用（连接池），避免批量刷新时反复建连；
- 单批失败只影响批内 code，其余照常返回，由调用方降级；
- 停牌 / 退市 / 代码不存在时价格字段为空，解析阶段直接丢弃，不会写入脏数据。

---

## 五、与小程序自选股页面的字段映射

页面列表项当前字段 → 接口返回：

| 前端字段 | 接口字段 | 说明 |
|---|---|---|
| `name` | `name` | 股票名称 |
| `code` | `code` | 展示格式 `"SH 600519"`，与前端一致 |
| `price` | `price_text` | 带千分位，如 `1,684.50` |
| `pct` | `pct_text` | 带符号，如 `+1.12%` |
| `up` | `up` | 涨跌幅 > 0 |
| `cost` | `cost_price` | 成本价（元） |
| `qty` | `quantity` | 持股数量（股） |
| `pending` | `pending` | 无行情时展示 `--` |
| — | `profit_amount` / `profit_rate` | 后端算好的浮动盈亏 |
| — | `stale` | true 表示价格来自旧缓存 |

前端 Tab 与 query 参数：`全部 → filter=all`、`涨幅榜 → filter=up`、`跌幅榜 → filter=down`。

---

## 六、curl 测试

> 先登录拿 token：
> `TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auth/login -H 'Content-Type: application/json' -d '{"code":"demo"}' | python -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")`

**1）正常添加（带成本价与数量）**

```bash
curl -s -X POST http://localhost:8080/api/v1/watchlist \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"stock_code":"SH600519","cost_price":"1680.50","quantity":100}'
```

**2）重复添加并更新成本价（Upsert，不报错）**

```bash
curl -s -X POST http://localhost:8080/api/v1/watchlist \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"stock_code":"SH 600519","cost_price":"1700.00","quantity":200}'
```

**3）无效代码（返回 400）**

```bash
curl -s -X POST http://localhost:8080/api/v1/watchlist \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"stock_code":"99999999"}'
```

**4）查询列表（涨幅榜）**

```bash
curl -s "http://localhost:8080/api/v1/watchlist?filter=up" -H "Authorization: Bearer $TOKEN"
```

**5）手动刷新（正常）**

```bash
curl -s -X POST http://localhost:8080/api/v1/watchlist/refresh -H "Authorization: Bearer $TOKEN"
```

**6）无自选股时刷新（返回空数组）**

```bash
# 换一个未添加过自选的账号 token 后再执行
curl -s -X POST http://localhost:8080/api/v1/watchlist/refresh -H "Authorization: Bearer $TOKEN2"
```

**7）新建提醒规则（字段与「新增提醒」弹窗一致）**

```bash
curl -s -X POST http://localhost:8080/api/v1/alerts \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"stock_code":"SH600519","stock_name":"贵州茅台","condition_type":"price_up","threshold":"1700.00","check_frequency":"close"}'
```

成交量异动额外带 `volume_base`：

```bash
curl -s -X POST http://localhost:8080/api/v1/alerts \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"stock_code":"SZ300750","stock_name":"宁德时代","condition_type":"volume","threshold":"1.5","volume_base":20,"check_frequency":"realtime"}'
```

**8）同股票同条件重复创建（返回 409）**

```bash
curl -s -X POST http://localhost:8080/api/v1/alerts \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"stock_code":"SH600519","stock_name":"贵州茅台","condition_type":"price_up","threshold":"1850.00"}'
```

**9）非法阈值（返回 400）**

```bash
curl -s -X POST http://localhost:8080/api/v1/alerts \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"stock_code":"SH600519","stock_name":"贵州茅台","condition_type":"pct_up","threshold":"120"}'
```

**10）越权改他人规则（返回 404）**

```bash
# 用 TOKEN2 去改 TOKEN 的规则 id=1
curl -s -X PUT http://localhost:8080/api/v1/alerts/1 \
  -H "Authorization: Bearer $TOKEN2" -H 'Content-Type: application/json' \
  -d '{"threshold":"1000.00"}'
```

**11）删除规则**

```bash
curl -s -X DELETE http://localhost:8080/api/v1/alerts/1 -H "Authorization: Bearer $TOKEN"
```

---

## 七、小程序对接

小程序工程在 `../stock-agent-miniapp`（与本目录平级）。

### 首次 clone 后必做：构建 npm

`miniprogram_npm/` 是开发者工具「构建 npm」的产物，**属于构建结果、没有入库**，
所以刚 clone 下来时它是空的。少了这个目录小程序会**白屏**
（TDesign 组件一个都找不到），必须先补上这一步：

```bash
cd stock-agent-miniapp
npm install
```

然后回到开发者工具：**工具 → 构建 npm**。

看到 `miniprogram_npm/tdesign-miniprogram/` 出现就可以了，再点编译即能正常渲染。

> 同理，`.gitignore` 里也忽略了 `node_modules/`，换机器都要先 `npm install`。
> 后端无需额外准备，`go run .` 会自动跑迁移建表。

**请求基地址**：`stock-agent-miniapp/config.js`。
小程序没有 `.env`，官方推荐用 `envVersion` 区分环境，我们照此办理：

| envVersion | 含义 | apiBase |
|---|---|---|
| `develop` | 开发版（开发者工具 / 真机调试） | `http://127.0.0.1:8080` |
| `trial` | 体验版 | `https://trial-api.example.com` |
| `release` | 正式版 | `https://api.example.com` |

换环境、换机器只改这一张表，其余文件一律从 `config` 取。

> 开发阶段需在开发者工具「详情 → 本地设置」勾选 **不校验合法域名**，
> 否则请求 `127.0.0.1` 会被拦截。正式上线前把地址换成已备案域名并去掉该勾选。

**Token**：`utils/request.js` 全权负责

1. 首次发请求前先 `wx.login()` 拿 code → `POST /api/v1/auth/login` 换 JWT；
2. JWT 写入本地缓存（`token` / `user_id`），后续请求自动带 `Authorization: Bearer <token>`；
3. 请求返回 **401 时静默重新登录一次并重放原请求**，用户不会看到「未登录」；
4. 并发请求共享同一个登录 Promise，不会重复触发 `wx.login`。

**调用约定**：页面只调 `services/`，不直接碰 `wx.request`。
`services/watchlist.js` 暴露 `fetchList` / `add` / `refresh` / `remove` 四个方法。
