# A股业务表设计与指南审阅

> 对《A 股数据业务表设计指南》的审阅结论 + 落到本项目的表结构推荐。
> DDL 以 `server/migrations/008_stock_master.sql`、`009_stock_finance_indicator.sql` 为准，本文只解释取舍。

## 0. 结论

- 指南的**信条可用**：存数据本体、不存渲染形态；LLM 只路由；业务表/向量库/卡片三者解耦。项目当前架构基本就是按这个思路长出来的。
- 指南的**具体参数不可照搬**：1024 维、`ts_code` 主键、主数据表混装估值字段、`daily_snapshot(card_json)` 四处与现状或原则冲突。
- 指南的**成本与权限章节大半不适用**：Embedding 本地自建、无 token 计费；agent 无 text2sql，且同一个 `DATABASE_URL` 还要写 checkpoint，不能整体切只读。
- 推荐落 **3 张表 + 1 个视图**（P0）：`stock_basic`、`stock_industry`、`stock_concept`、`v_stock_profile`；`stock_finance_indicator` 列 P1。
- 比表结构更该先修的是**两个口径债**：股票代码三套写法并存（含 920 前缀判定分歧）、金额单位 `total_mv` 元/万元并存。

> 实施状态：迁移、主数据同步、Go 搜索兜底与资料接口、agent 同业工具与只读连接池已落地；
> RAG 行业过滤与财务指标切表仍待做。见 §10。

## 1. 指南逐节审阅

| 章节 | 判定 | 本项目的调整 |
|---|---|---|
| §1 数据分层 | 保留 | 与 `docs/ARCHITECTURE.md` §3、`docs/ASHARE_VECTORIZATION.md` §1 一致；实时行情继续只走 Redis 30s 快照 |
| §2.1 `stock_basic` | 改写 | 只留身份字段；`total_mv/pe_ttm/pb/dividend_yield` 移出（每日变动，落库即过时） |
| §2.2 `stock_concept` | 保留 | 补 `board_code`、`source`、`updated_at`，否则无法做整块刷新与多源区分 |
| §2.3 `stock_finance_indicator` | 保留但降级 | 列 P1；字段与 `agent/tools/fundamental.py` 输出对齐，单位写进列名 |
| §2.4 向量 metadata 约定 | 保留 | 已实现（`ashare_documents` + `chunks.metadata`），无需新表 |
| §3 建表五条铁律 | 保留并补两条 | 补「单位进列名」「代码单一写法」，这两处项目已经踩过 |
| §4 前端卡片解耦 | 保留原则，弃用形态 | 项目已是 `card/table` 事件 + payload，不引入第二套 `structured_data` 信封 |
| §4 尾部 `daily_snapshot(card_json)` | 剔除 | 正是「存渲染形态」的反例；历史复盘应存结构化行，前端重新渲染 |
| §5 LLM 只当路由器 | 保留 | 已实现（`classify` → `route_by_intent`） |
| §6.1/6.2 只读角色 + 字段白名单 SQL 工具 | 剔除 | 项目无 text2sql；工具全是确定性函数。将来要加再单独建只读连接（见 §9） |
| §6.3 四层防护 | 部分保留 | 「只读 / 参数化 / 结果限长」沿用；agent 写 checkpoint/会话/审计的角色不能动 |
| §7 空间账（40GB） | 剔除重算 | 40GB 按当前 1536 维与真实数据量重新估算（见 §6） |
| §8 成本参考 | 剔除 | 本地 Qwen3-Embedding，无按 token 计费；模型组合也与本项目的 LLM 配置无关 |
| §9 一句话收尾 | 保留 | 与本项目「后端原子数据、前端换衣服」的既有约定一致 |

### 1.1 可迁移的四条判断

1. **数值与文本分家**：能算的字段进表，靠语义的进向量库。项目已按此切分（报表数字不向量化）。
2. **写入方固定**：业务表由定时任务/离线批处理写，agent 只读。比给 LLM 加十层防护更有效。
3. **元数据要能过滤**：向量块自带 `stock_codes/source_type/pub_time`，检索先过滤再算距离；项目已实现。
4. **表按「怎么查」设计**：`stock_industry` 的主键顺序、`stock_concept` 的正反查索引都按查询路径定，不按展示顺序定。

## 2. 项目现状：决定表结构的六个硬约束

1. **代码写法三套且已分叉**
   - Go 规范：`SH600519`（`server/internal/domain/stockcode/code.go` 的 `NormalizeCode`），`user_watchlist.stock_code` 存的就是它。
   - ingest / RAG：**6 位裸码**（`ashare_documents.stock_codes TEXT[]`，`retriever` 过滤前再 `digits()`）。
   - Python 工具：`600519` 取数、`600519.SH` 进卡片 note。`to_market()` 按首位判交易所，`920xxx` 被当成 `sz`，而 Go 的 `MarketOf` 判 `BJ`——同一只北交所新股，两边会拼出不同 URL。
2. **向量维度已定 1536**：`chunks.embedding vector(1536)` + `agent/config.py` 的 `EMBED_DIM`，三处必须一致；指南的 1024 属于另一套迁移，不采纳。
3. **行情不落库**：腾讯快照 + Redis 30s（`quote.go`）、akshare 实时拉。指南自己也说「实时行情不存」，一致。
4. **无 SQL 工具**：`agent/tools/__init__.py` 注册的 12 个工具全是 HTTP / akshare / 固定 SQL 仓储调用，没有任何 nl2sql 路径。
5. **迁移机制两套并存**：`/docker-entrypoint-initdb.d` 只在空卷执行；用户表另有 GORM `AutoMigrate`；`ashare.cli init-db` 硬编码 007 且**按分号切语句**，不能写 `$$` 函数体。
6. **卡片契约已固定**：`agent/subgraphs/cards.py` 定义 9 种 card/table 载荷，前端按类型渲染；新表不应产生新卡类型。

## 3. 数据分层建议

| 数据 | 放哪 | 现状 | 本次建议 |
|---|---|---|---|
| 股票代码/名称/板块/上市退市/ST | PG `stock_basic` | 只在 `universe.json`（TTL 7 天）+ 腾讯搜索接口 | **新建** |
| 行业分类 | PG `stock_industry` | 文档级单值 `ashare_documents.industry`（源侧自由文本） | **新建** |
| 概念板块 | PG `stock_concept` | 无 | **新建** |
| 财报比率 + 卡片用关键科目 | PG `stock_finance_indicator` | 每次问答实时拉新浪（慢、易抖动） | P1 |
| 年报/公告/研报/快讯 | `chunks` + `ashare_documents` | 已落地 | 维持 |
| 日 K / 分钟 K | 不落库 | baostock/东财实时取 | 维持（全市场回测另立项目） |
| 实时行情、涨跌幅、估值快照 | 不落库 | Redis 30s + 腾讯/东财 | 维持 |
| 卡片 HTML / 拼好的文案 / card JSON 快照 | 不落库 | 前端渲染 | 维持（明确否决 `daily_snapshot`） |

## 4. P0：主数据表

### 4.1 `stock_basic`

| 列 | 类型 | 说明 |
|---|---|---|
| `code` | `VARCHAR(6)` PK | 6 位裸码，`CHECK (~ '^[0-9]{6}$')`。选裸码是因为下游两个大头（ingest/RAG、akshare 取数）都用裸码 |
| `market` | `VARCHAR(2)` 生成列 | 由前缀推导 `SH/SZ/BJ`；未知前缀（如 B 股 `900/200`）取不到值，被 `NOT NULL` 挡在入库前 |
| `board` | `VARCHAR(8)` 生成列 | `main/gem/star/bse`，用于涨跌幅规则与筛选 |
| `name` | `VARCHAR(32)` | 不建唯一约束：更名、A/B 股并存会重名，用 trigram 模糊匹配更合适 |
| `pinyin` / `pinyin_abbr` | `VARCHAR(32/16)` | 仅服务 `symbol_search` 的本地兜底，可为空 |
| `list_date` / `delist_date` | `DATE` | |
| `status` | `VARCHAR(8)` | 只记 `listed/delisted`；停牌是实时状态，不进表（否则要分钟级同步） |
| `is_st` | `BOOLEAN` | 名称派生标记，问答里「排除 ST」是高频过滤 |
| `source` / `updated_at` | | 审计用 |

关键写法（`market` 生成列）：

```sql
market VARCHAR(2) GENERATED ALWAYS AS (
  CASE
    WHEN left(code,3) IN ('600','601','603','605','688','689') THEN 'SH'
    WHEN left(code,3) IN ('000','001','002','003','300','301') THEN 'SZ'
    WHEN left(code,3) = '920' OR left(code,2) IN ('43','83','87','88') THEN 'BJ'
  END) STORED
```

新增代码段只改这一处，Python/Go 不再各写一份前缀表。

**不放的字段**：`total_mv`、`pe_ttm`、`pb`、`dividend_yield`、`is_hs`、`area`。前四个是每日变动数据；后两个当前业务没有对应工具（无陆股通、无地域筛选），需要时 `ALTER TABLE ... ADD COLUMN` 即可。

### 4.2 `stock_industry`

`PRIMARY KEY (code, std)`，`std` 区分数据源：东财 `em`、新浪 `sina`（后者只用于概念）；`l1/l2/l3` 分行存，不拼成 `"食品饮料-白酒"`（指南 §3 自己反对拼串，§2.1 却又拼了行业串）。

取数三级优先，越靠前请求越少、覆盖越全：

1. **东财 datacenter 整表**（`RPT_F10_BASIC_ORGINFO` 的 `EM2016`）：5 页约 2 秒、覆盖全市场、自带 `食品饮料-饮料-白酒` 三级；
2. **东财 push2 板块循环**：84 个板块逐个取成分，只有 `l1`；
3. **新浪板块**：证监会口径 84 个板块，覆盖约 86%（4813/5577），只在前两条都挂时兜底。

push2 会整段拒绝国内云主机 IP（`RemoteDisconnected`，换域名无用），但 datacenter 是不同子域、不受影响；同花顺不在候选里（akshare 只有板块清单与行情、没有成分接口），申万成分页连续请求会被判 `No tables found`。三套词表不共存——换源成功后删掉另一源的行，同一只股票只保留一条现役行业；`std` 就是把这件事记下来的地方。

索引 `(std, l1, l2)` 服务「某行业全部股票」和同业对比。

### 4.3 `stock_concept`

`PRIMARY KEY (concept, code)`：主键首列给「概念 → 成分股」（高频），另建 `(code)` 索引给「这只股票有哪些概念」（`v_stock_profile` 用）。`board_code` 存板块代码（东财是数字代码，新浪用 `gn_*` label），`source` 记数据源（`em` / `sina`），便于按板块整块刷新。

### 4.4 `v_stock_profile`

只读视图，一次给全三种代码写法与聚合结果，调用方不再自己拼：

| 输出列 | 用途 |
|---|---|
| `code` / `market` | 裸码、交易所 |
| `ts_code` = `code.market` | LLM 上下文、卡片 note |
| `gw_code` = `market+code` | Go 网关、`user_watchlist` 对齐 |
| `industry_l1/l2/l3` | 行业过滤、同业候选 |
| `concepts` `TEXT[]` | 概念标签、主题检索 |

## 5. P1：`stock_finance_indicator`

- 主键 `(code, report_date)`，另存 `report_type`、`announce_date`（as-of 语义，避免用未披露数据回答历史问题）。
- 字段与 `agent/tools/fundamental.py` 的卡片输出一一对应：`eps_yuan/bps_yuan/roe_pct/gross_margin_pct/net_margin_pct/debt_asset_pct/revenue_yoy_pct/netprofit_yoy_pct`，外加财报卡需要的 `revenue_yuan/netprofit_yuan/ocf_yuan`。
- **比率是累计口径**（Q3 = 前三季度），不是单季；跨期比较前必须先确认。
- 不存完整三大报表：列多、口径杂、利用率低，需要更多科目再加列或回落接口。

量的估算：5500 × 40 期 ≈ 22 万行，约 50 MB（含索引）。对比 `chunks` 的 8~12 KB/块，这张表基本免费。

## 6. 明确不建的表

| 候选 | 不建的理由 |
|---|---|
| 日 K / 分钟 K 表 | `get_history_daily` 走 baostock，全市场 10 年约 1300 万行且要处理复权；当前没有图表以外的用法 |
| 实时行情表 | 30s 就过时，网关已有 Redis 缓存 |
| 估值日频快照 `stock_daily_basic` | 单股历史估值 `get_valuation` 已能实时取；只有「全市场按 PE 分位筛」才值得建，届时按年分区 |
| `daily_snapshot(trade_date, ts_code, card_json)` | 存的是渲染形态，前端改版即报废；要复盘就存结构化行 |
| 新闻/研报观点表 | `ashare_documents` 已有 `title/org/rating/url/pub_time/event_type`，重复建表只会分裂检索面 |
| 用户/自选/提醒 | 已有 `users`/`user_watchlist`/`alert_rule` |

## 7. 命名与单位规范（指南五条 → 七条）

| 原则 | 正例 | 反例 |
|---|---|---|
| 字段原子化 | `total_mv` 存数值 | `"市值1.2万亿"` |
| 不存展示形态 | 只存数据 | `card_color`、`font_size` |
| 不存拼接结果 | 分存 `code`/`name` | `"茅台(600519)"` |
| 时间用时间类型 | `DATE` / `TIMESTAMPTZ` | `"3月28日"`、`"20260328"` |
| 数值用数值类型 | `NUMERIC` / `BIGINT` | `VARCHAR` 存万亿 |
| **单位进列名** | `total_mv_yuan`、`amount_wan`、`roe_pct` | 裸 `total_mv`（现状：Go=元、akshare=万元） |
| **代码单一写法** | 库内 6 位，出库用视图转换 | 同一列混存 `SH600519` / `600519.SH` |

> 已有债不强行改名：`chunks`/`quote` 的 `total_mv` 语义由各自的注释和前端换算兜着。**新表不再新增同名异义字段**。

## 8. 数据来源与刷新节奏

| 表 | 源 | 频率 | 坑 |
|---|---|---|---|
| `stock_basic` | `ak.stock_info_a_code_name()`（`ingest/ashare/universe.py` 已在用） | 日更 | 接口不含退市股：只能置 `status='delisted'`，**不要 DELETE**（FK 是 CASCADE，会带走行业/概念） |
| `stock_industry` | 东财行业板块列表 + 板块成分 | 周更 | 板块成分含 B 股/退市股，入库前按 `stock_basic` 过滤，否则外键报错 |
| `stock_concept` | 东财概念板块列表 + 板块成分 | 周更 | 概念数百个、成分每日变；限速 2 QPS、全量约 4~5 分钟，可先用 `--concept-limit 50` 跑前 N 个（源侧排序，不做成交额重排） |
| `stock_finance_indicator` | 新浪指标 + 报表接口（`fundamental.py` 已验证可用） | 季报后一周回补 | 指标接口只覆盖近 3 年，历史回补要按年多次拉 |

刷新用「先 upsert、再按 `updated_at < 本轮开始时间` 清理」的方式处理退榜标的，不要每轮 `TRUNCATE`（会锁表且丢 `updated_at` 语义）。
上面三类由 `ingest/ashare/master.py` 实现，命令见 §10。

## 9. 与现有代码的对接点

- **Go 网关**（已实现）：新表只读，**不并入** `db.AutoMigrate`（`user_watchlist` 已经被 001 SQL 和 GORM 同时定义，再来一次迟早分叉）。
  - `GET /api/v1/stocks/search`：腾讯 smartbox 为主路，上游失败或空结果时用 `stock_basic`（代码前缀 + 名称/拼音三元组）兜底；响应头 `X-Search-Source: tencent|db|empty` 标明来源。
  - `GET /api/v1/stocks/profile`：读 `v_stock_profile`，Redis 缓存 1h，代码支持三种写法。
- **Agent**（已实现）：
  - 新增工具 `get_industry_peers`（`agent/tools/peers.py` → `agent/repo/stock.py`）：按东财行业查同业，供模型挑可比公司。
  - `get_peer_compare` 的 `peers` 留空时自动补两家（`peers_source=industry`），只是兜底——按代码序取的不一定是最合适的可比对象。
  - 只读连接池 `agent/db.py`：池内连接固定 `default_transaction_read_only=on`，与 checkpointer / 会话 / 审计的写连接分开；建池失败由工具层降级，不拖住对话。
  - 新工具**不进** `_DATA_TOOLS`（它不产数字，进了会让「没取到数据」的兜底误判为已取数）；`top_n` 这个参数名也是为了避开「code+limit 判成资讯」的文本调用还原规则。
- **RAG**（待做）：代码过滤维持裸码不变；**行业过滤不要直接 join `ashare_documents.industry`**（源侧自由文本，如「白酒」，与东财「酿酒行业」不是一套词表），正确路径是 `stock_codes → stock_industry` 反查后再过滤。
- **小程序**：不动。本次不新增卡片类型，也不改 payload 形状。
- **运维**：`ashare.cli init-db --file 008_stock_master.sql` 可手工执行迁移（`--file` 可重复）；迁移里仍不要写 `$$` 函数体，它按分号切语句。

## 10. 落地与验收

已完成（代码层）：

1. 迁移 `008_stock_master.sql`、`009_stock_finance_indicator.sql`。
2. 数据导入：`python -m ashare.cli sync-master`（`--only basic|industry|concept`、`--concept-limit N`）。
3. Go：搜索兜底 + 资料接口，单测 `go test ./internal/module/stock/...`。
4. Agent：只读池 + 同业工具，单测 `python -X utf8 tests/test_master_data.py`。

上线待做：

1. 已有库手工跑迁移（空数据卷由 initdb 自动执行）：
   `python -m ashare.cli init-db --file 008_stock_master.sql --file 009_stock_finance_indicator.sql --file 010_stock_profile_any_source.sql`
2. 首次同步：先 `--only basic`，再全量；概念全量约 4~5 分钟。之后每天 basic、每周 industry/concept。
3. 跑下面的验收 SQL。
4. 观察两周：确认同业候选质量、搜索兜底命中率，再决定是否把财务指标读取切到 009。

```sql
-- 行业覆盖率：上市股里没有行业的不应超过 1%（不绑 std，换源后照样能查）
SELECT count(*) FILTER (WHERE b.status = 'listed')        AS listed,
       count(*) FILTER (WHERE i.code IS NULL)             AS no_industry
FROM stock_basic b
LEFT JOIN stock_industry i ON i.code = b.code;

-- 交易所/板块分布：market/board 是生成列，未知前缀进不来，这里应始终无 NULL
SELECT market, board, count(*) FROM stock_basic GROUP BY 1, 2 ORDER BY 3 DESC;

-- 文档里的代码有多少在本表找不到（应 < 2%，超出说明 universe 漏了退市/B股/指数）
SELECT DISTINCT c
FROM ashare_documents a, unnest(a.stock_codes) AS c
WHERE NOT EXISTS (SELECT 1 FROM stock_basic b WHERE b.code = c)
LIMIT 20;
```

回滚顺序：`DROP VIEW v_stock_profile;` → `DROP TABLE stock_finance_indicator;` → `DROP TABLE stock_concept;` → `DROP TABLE stock_industry;` → `DROP TABLE stock_basic;`

## 11. 待决策

1. 申万行业要不要补：当前是东财 `std='em'` 主源 + 新浪 `std='sina'` 兜底；两套词表差异大，补申万之前先确认 RAG 行业过滤用哪一套，别把两套混进一个 `l1`。
2. 财务指标由 `ashare` 常驻进程维护，还是独立 cron（前者能复用限速与运行审计）。
3. `universe.json` 是否彻底下线，全部改查库（会影响 `em-news` 这类按股票采集的源）。
4. 数据库只读角色何时引入：现在用「只读连接池 + 会话级 `default_transaction_read_only`」兜住误写；等 text2sql 真正立项再建 `stock_ro`，并新增 `DATABASE_URL_RO`，**不要**直接改 `DATABASE_URL`（会连 checkpoint 一起断掉）。
5. 日 K / 估值快照的触发条件：出现全市场筛选或回测需求再建，届时用分区表而非普通表。
