"""全局配置：全部走环境变量，本地开发给默认值。"""
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/stock_agent")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ===== 主数据只读连接池 =====
# 与 checkpointer / 会话 / 审计的写连接分开：池内连接固定 default_transaction_read_only=on，
# 工具即使写错语句也改不了数据。没有单独建 ROLE，因为 LLM 不持有凭据、也没有 SQL 通道，
# 这一层防的是「我们自己的代码写错」。
DB_RO_POOL_MAX = int(os.getenv("DB_RO_POOL_MAX", "4"))
# 建池超时：库不可用时快速失败，交给工具层降级，不拖住整轮对话
DB_RO_TIMEOUT = float(os.getenv("DB_RO_TIMEOUT", "5"))

# ===== 数值类取数：业务表优先，实时接口兜底 =====
# auto=业务表新鲜且命中就返回，缺失/过期回落实时接口；db=只读业务表（排障用）；
# live=只打实时接口（一键回滚开关，改完不用重新发版）
FINANCE_SOURCE = os.getenv("FINANCE_SOURCE", "auto").strip().lower()
# 「新鲜」的判据是同步任务最近一次成功距今的小时数，而不是最新报告期距今的天数：
# 报告期本身有披露滞后（Q3 到 10 月底才发完、年报到次年 4 月），按日期差会周期性误判。
FINANCE_SYNC_MAX_AGE_HOURS = int(os.getenv("FINANCE_SYNC_MAX_AGE_HOURS", "48"))
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
SMALL_MODEL = os.getenv("SMALL_MODEL", "gpt-4o-mini")
# 在线（agent 检索 query）用的 embedding 模型；必须与 ingest 的 OFFLINE_EMBED_MODEL
# 同模型同维度，否则写入与检索落在两个向量空间
ONLINE_EMBED_MODEL = os.getenv("ONLINE_EMBED_MODEL", "Qwen/Qwen3-Embedding-4B")
# 输出维度：Qwen3 系列支持 dimensions 降维。1536 是保住 HNSW 索引的上限内取值
# —— pgvector 对 vector 类型的 HNSW 索引硬限 2000 维，2048/2560 直接报错
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))

# 工具超时
TOOL_TIMEOUT = float(os.getenv("TOOL_TIMEOUT", "8.0"))
# LLM 单次请求超时（秒）：约束单次请求的完成时间；不设的话上游卡死会挂住整条事件流
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))
# 小模型（改写/分类/判读）单次超时：这些调用只处理短输入，60s 等满没有意义
SMALL_LLM_TIMEOUT = float(os.getenv("SMALL_LLM_TIMEOUT", "30"))
# 工具决策调用的单独超时：它只输出 tool_calls，不需要等满生成模型的 60s
TOOL_CALL_LLM_TIMEOUT = float(os.getenv("TOOL_CALL_LLM_TIMEOUT", "30"))
# LLM 重试次数：OpenAI SDK 默认 2，配合 60s 超时会把一次上游卡顿放大成 60~180s。
# 默认只重试 1 次，既保留瞬时抖动恢复能力，也避免 68s 这类长尾。
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))
# 生成类回答是否开启模型思考。上游 DeepSeek 默认先推理再出字，reasoning 不计入
# 正文流：实测 T-01/T-02 的首 token 被 900~2700 个推理 token 拖到 5.5~29s。
# 关闭后首 token 降到 0.6~1.8s，回答仍完整（答案质量以 benchmark 规则断言为准）。
# 只作用于 chat / rag / stock synthesize 三个「作答」节点；tool_call 决策保持默认。
ANSWER_ENABLE_THINKING = os.getenv("ANSWER_ENABLE_THINKING", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
# 决策类小模型（改写/分类/判读/澄清/摘要）是否开启思考。生产 SMALL_MODEL 与主力模型
# 同款（DeepSeek），默认开启推理：一次判读要先吐几百个隐藏 token 才给结论（judge_ms ~2.3s）。
# 这些节点只输出短结构（意图/布尔/一句反问），关掉思考不改判定口径；
# 需要回滚时把该变量置 true，行为即恢复为上游默认。
SMALL_LLM_ENABLE_THINKING = os.getenv("SMALL_LLM_ENABLE_THINKING", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
# RAG 相关度阈值：针对余弦相似度（不是 RRF 融合分）。
# 仅当重排关闭时生效。Qwen3 向量空间的地板值很高（无关 query 与语料的平均相似度
# 也有 0.56，实测相关/无关的 top 相似度完全重叠），单靠它只能做粗筛，
# 所以默认走重排分判定，见下面的 RERANK_THRESHOLD。
RAG_THRESHOLD = float(os.getenv("RAG_THRESHOLD", "0.2"))

# ===== 重排（交叉编码器，直接给 query↔chunk 打相关性分）=====
# 开启后 retriever 取 RERANK_CANDIDATES 个候选送 /v1/rerank，按重排分排序并截断到 k；
# 命中闸也换成重排分（实测相关 ~0.99、无关 0，可干净切分）
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
RERANK_MODEL = os.getenv("RERANK_MODEL", "Qwen/Qwen3-Reranker-0.6B")
# 送重排的候选数：太小漏召回，太大徒增延迟（重排是逐对推理）
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "20"))
RERANK_THRESHOLD = float(os.getenv("RERANK_THRESHOLD", "0.5"))
RERANK_TIMEOUT = float(os.getenv("RERANK_TIMEOUT", "8"))
# 重排接口 429/5xx 退避重试次数：N=100 时单请求 token 翻倍，实测会触发上游限流；
# 不重试会静默回退 RRF，等于这一段没开重排
RERANK_MAX_RETRIES = int(os.getenv("RERANK_MAX_RETRIES", "2"))
# 进程内同时进行中的重排请求上限。实测 N=100、4 路并发时 37% 的请求被上游 429 打回，
# 失败后静默回退 RRF（质量掉档但不报错）。限流到 2 路可把突发摊平；0 表示不限。
RERANK_CONCURRENCY = int(os.getenv("RERANK_CONCURRENCY", "2"))
# 进上下文的片段数（上线数量）；重排候选池见上面的 RERANK_CANDIDATES。
# 实测（2026-10-26 sweep，120 正/20 负）：N=100 时 k=8 比 k=6 的 Hit 高 2.5pt，
# 上下文只多约 30%，故默认取 8。
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "8"))
# ===== 检索候选选择模式 =====
# chunk：RRF 前 N 块直接送重排（旧行为）；
# doc_first：先按文档聚合 RRF 排名，选 Top D 篇文档，再展开每篇的若干块，
# 防止同题材研报的多块占满候选池、把目标文档挤出前 30（语料扩充后实测的主要问题）
RETRIEVAL_MODE = os.getenv("RETRIEVAL_MODE", "chunk").strip().lower()
# doc_first 模式先从这么大的 RRF 池里按文档聚合（必须 >= RERANK_CANDIDATES）
DOC_FIRST_POOL = int(os.getenv("DOC_FIRST_POOL", "300"))
# 最多选多少篇文档
DOC_FIRST_DOCS = int(os.getenv("DOC_FIRST_DOCS", "30"))
# 每篇文档最多展开多少块（候选池层面；最终上下文仍受 RAG_PER_DOC 限制）
DOC_FIRST_PER_DOC = int(os.getenv("DOC_FIRST_PER_DOC", "5"))
# 同一文档最多进几块：按 doc_id 限流（按标题会把一篇年报压到 2 块）
RAG_PER_DOC = int(os.getenv("RAG_PER_DOC", "3"))
# 重排后按 RERANK_THRESHOLD 再裁一次上下文。默认关：历史行为是「只判是否命中，
# 不裁剪片段」，开启后低分片段不再进 prompt/sources，代价是可能裁到 <2 条触发升级
RAG_RERANK_FILTER = os.getenv("RAG_RERANK_FILTER", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
# 高分短路：最高重排分达到该值时跳过 LLM judge 判读，直接按命中处理。
# 实测语料上相关样本 top1 中位 1.0、语料外负样本最大值 0.884，0.9 能省掉
# 约 95% 的 judge 调用（每次 ~2.3s）且不误放负样本；0 表示关闭短路。
RAG_JUDGE_SKIP_SCORE = float(os.getenv("RAG_JUDGE_SKIP_SCORE", "0.9"))
# Go 网关地址（工具调用回现有后端获取行情）
GO_SERVER_URL = os.getenv("GO_SERVER_URL", "http://127.0.0.1:8080")
# 调 Go 网关受保护接口用的服务间 token（Agent 作为可信内部服务）
GO_SERVER_TOKEN = os.getenv("GO_SERVER_TOKEN", "")

# ===== 工具层 =====
# 同一工具 + 同一参数的进程内缓存时长（秒），0 表示关闭。
# 取数工具都是只读且按日更新，akshare 单次 1-3s，短 TTL 只省「同一天被反复问」
TOOL_CACHE_TTL = int(os.getenv("TOOL_CACHE_TTL", "300"))
# 盘口类（行情/估值/资金流/同业快照）单独一档：这几项盘中随价格变，
# 套 5 分钟缓存会让用户看到旧价；30s 与 Go 网关侧行情缓存对齐
TOOL_CACHE_TTL_REALTIME = int(os.getenv("TOOL_CACHE_TTL_REALTIME", "30"))
# 缓存条目上限（LRU）：防止长时间运行把不同股票的结果都攒在内存里
TOOL_CACHE_MAX = int(os.getenv("TOOL_CACHE_MAX", "256"))
# 失败结果短 TTL：数据源整站连不上时，同一轮里反复重试只会重复等待，
# 30s 内直接复用失败结论；成功结果仍按上面的档位缓存，失败不会污染正常缓存
TOOL_CACHE_TTL_FAILURE = int(os.getenv("TOOL_CACHE_TTL_FAILURE", "30"))
# 同一轮内并发执行几个工具：akshare 走线程池，全放开会让多个 DataFrame 同时驻留
TOOL_CONCURRENCY = int(os.getenv("TOOL_CONCURRENCY", "4"))
# 单轮工具总预算（秒）：tool_execute 里多个工具并发执行，等全部返回会让最慢的那个
# 决定首屏——单工具本身是「8s 超时 + 重试 1 次」，最坏 16s+。超过该预算的调用按
# 超时降级，已完成的结果照常进 synthesize。默认与单工具超时对齐（8s），只砍掉
# 「失败后重试」造成的翻倍长尾；想进一步压尾可下调到 5~6s（代价是慢速历史行情可能被截）。
# 0 表示不设预算（恢复旧行为）。
TOOL_ROUND_BUDGET = float(os.getenv("TOOL_ROUND_BUDGET", "8"))

# 数据源：卡片数据来自 akshare（东财/新浪）+ baostock（历史日 K），均免 token。
# TUSHARE_TOKEN / TUSHARE_API 已废弃删除——原 token 对 moneyflow/top_list 等接口无权限（40203）。
# akshare 抓公开页面，Docker 内直连即可；本机调试若走系统代理失败，进程内设 NO_PROXY=* 绕过。

# ===== LangGraph checkpoint 连接池 =====
# 单 worker 最多占这么多条 PG 连接。并发上限只有 3（Go 网关限流），
# 池子开大既用不上，又会在 3GB 机器上白占服务端内存
CHECKPOINT_POOL_MAX = int(os.getenv("CHECKPOINT_POOL_MAX", "4"))
# 应用表（chat_sessions / agent_runs）写连接池：与 checkpoint 池分开，
# 避免会话列表查询和 saver 抢同一条连接
APP_POOL_MAX = int(os.getenv("APP_POOL_MAX", "4"))

# 对话 token 超过该值触发滚动摘要
SUMMARY_TOKEN_LIMIT = int(os.getenv("SUMMARY_TOKEN_LIMIT", "6000"))

# ===== checkpoint 后台清理 =====
# 孤儿线程（benchmark / 调试等不在 chat_sessions 里的 thread）最后活跃超过该天数
# 即清理；用户会话不在此列（由 SESSION_KEEP_MAX 与手动删除管理，避免误删历史）
CHECKPOINT_ORPHAN_DAYS = int(os.getenv("CHECKPOINT_ORPHAN_DAYS", "1"))
# 后台清理间隔（小时）；0 表示启动后只跑一次。durability="exit" 已把每轮行数
# 压到 1 份，这里只负责回收跑批/调试留下的线程，防止行数随跑批累积
CHECKPOINT_CLEANUP_INTERVAL_HOURS = int(os.getenv("CHECKPOINT_CLEANUP_INTERVAL_HOURS", "24"))

# ===== 会话历史（chat_sessions 注册表）=====
# 单用户保留的会话数上限：超出自动淘汰最旧的（删注册行 + checkpoints + 摘要）。
# 限额取足，正常使用碰不到；防的是脚本/误用导致的会话数无限增长
SESSION_KEEP_MAX = int(os.getenv("SESSION_KEEP_MAX", "50"))
# 历史对话列表一次最多返回的会话数
SESSION_LIST_MAX = int(os.getenv("SESSION_LIST_MAX", "50"))

# 单次 LLM 请求允许的最大输入 token（保守上限，留足余量）。
# 上游上下文是 163840，超了直接返回 400：'number of input tokens ... has exceeded max_seq_len'，
# 这类错误冒泡会让整轮回答变成空，故发送前先按这个预算裁剪
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "60000"))
