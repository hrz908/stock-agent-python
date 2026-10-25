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
# 进上下文的片段数（上线数量）；重排候选池见上面的 RERANK_CANDIDATES
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "6"))
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
# 同一轮内并发执行几个工具：akshare 走线程池，全放开会让多个 DataFrame 同时驻留
TOOL_CONCURRENCY = int(os.getenv("TOOL_CONCURRENCY", "4"))

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
