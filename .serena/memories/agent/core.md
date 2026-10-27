# agent/（Python 智能体）

代码在 `src/agent/`（包名 `agent`）；LangGraph 主图在 `agent/core/graph.py`，问答事件流在 `agent/core/events.py`（由 `gateway.adapters.agent_bridge` 消费）。不再有独立 HTTP 入口。

## 主图（2026-10-23 LangGraph 架构改造后）
guardrail → rewrite → classify → 按 intent 分流：
- stock：先进 `subgraphs/rag.py` 漏斗（阈值 RAG_THRESHOLD + `prompts/rag_judge.xml` LLM 判读两级），命中带 [n] 引用作答；未命中把 intent 改写成 stock_agent，重路由到 `subgraphs/stock.py`（有界 ReAct：tool_call → tool_execute → synthesize，最多 3 轮）取数作答。
- chat：`subgraphs/chat.py` 直接流式陪聊；off_topic：`nodes/refuse.py` 固定话术婉拒（不调 LLM）；clarify：反问 / 护栏降级。
- `mcp` 作为意图名已废弃（曾与 Model Context Protocol 混淆），现在只有 MCP 协议语境才出现 mcp。

## 结构
- `nodes/`：guardrail / rewrite / classify（数据关键词快路径直接判 stock，省一次 LLM 调用）/ clarify / refuse / post（滚动摘要 + 免责声明 _STOCK_INTENTS={"stock","stock_agent"}）
- `subgraphs/cards.py`：卡片 pydantic 校验闸门，校验失败只丢那张卡、正文照常（双模式输出降级）
- `tools/`：10 个取数 @tool（Go 网关 / akshare / baostock，免 token，无条件注册）；`search_knowledge_base`（知识库检索，不进 _DATA_TOOLS 白名单）；`mcp_loader.py`（工具源：MCP Server 优先、失败整段回退直连，env STOCK_MCP_ENABLED）
- `mcp_servers/stock_server.py`：FastMCP stdio，重导出上述 10 个工具（functools.wraps 保签名/文档）
- `retriever.py`：pgvector 向量 + PG 全文混合检索，RRF(k=60)，阈值按 cosine similarity（默认 0.2）
- `checkpointer.py`：AsyncPostgresSaver + 连接池

## MCP 依赖坑（2026-10-23 实测）
- langchain-mcp-adapters 0.3.1 **禁止** `async with MultiServerMCPClient(...)`（直接 NotImplementedError）；持久会话用 `client.session(server_name)` 手动 `__aenter__` + `await load_mcp_tools(session)`，进程退出 `__aexit__`。
- mcp python-sdk 必须 `<2`：2.x 删了 `mcp.shared.context.RequestContext`，adapters 导入即 ImportError。

## 事件协议
`token` / `card` / `table` / `sources` / `tool_start` / `tool_end` / `error` / `done`；结构化数据走独立 card/table 事件，不进 token 流。main.py 只透传 custom 与 updates 模式，messages 模式不产出 token——节点内要显式 emit("token")。

## 本地验证
- venv：`agent/.venv`（uv 建，py3.13.16，全依赖）；根 `.venv` 是 py3.14，装不了 langchain。
- Windows 控制台必须 `-X utf8`，否则中文 print 触发 cp1252 UnicodeEncodeError。
- 回归：`python -X utf8 tests/regression/test_regression.py`（内置强制 STOCK_MCP_ENABLED=0）；MCP 联调：`python -X utf8 tests/smoke_mcp_tools.py`。
- 本地直起 uvicorn 时 psycopg 在 ProactorEventLoop 下不可用 → checkpointer warmup 失败（被容错吞掉，/healthz 正常）；Docker/Linux 无此问题。

## 部署
容器内 `GO_SERVER_URL` 用 `http://host.docker.internal:8080`；Dockerfile `COPY . .` 已含 mcp_servers/；requirements.txt 已锁 mcp<2 + langchain-mcp-adapters。

## 联调发现的坑（2026-10-23）
- tool_results 跨「用户轮次」累积：operator.add reducer + checkpointer 会把历史轮次结果留存，synthesize 会重发历史卡片（实测同 thread 第 2 轮 1→3 张、跨次运行 9 张）。修法：主图入口 guardrail 返回 {"tool_results": Overwrite([])}（langgraph.types.Overwrite 绕过 reducer）按轮清零；一轮内多跳取数照常累加。
- MCP adapters 0.3.1 返回形状：成功路径是 content 块列表 [{"type":"text","text":"{...}"}]，失败路径是 dict；mcp_loader._as_envelope 必须剥到文本再解析 JSON，否则卡片校验全拦。
- 确定性文案（无数据兜底/护栏降级/_OVERFLOW_REPLY/_FALLBACK）必须 emit("token")：main.py 只透传 custom 事件，messages 模式不转发，只返回 AIMessage 前端是空白（实测 Q5 EMPTY）。
- uvicorn 0.54 在 Windows 硬编码 asyncio.ProactorEventLoop（连策略都不认）→ 本地用 dev_server.py：自己建 SelectorEventLoop，直接 asyncio.run(server.serve(), loop_factory=...)。
- Go 网关 config.Load 曾用 *viper.ConfigFileNotFoundError 做 errors.As（viper v1.x 是值类型）→ 无 config.yaml 直接 Fatal；已改为值类型匹配。Go 侧不再加载 .env，本地启动需先把 server/.env 注入进程环境。

## 联调启动（Windows 本地全链路）
1) server/ 下 docker compose up -d postgres redis；2) go build -o stock-agent-server.exe . 并带 env 启动；3) agent 下 .venv/Scripts/python.exe -X utf8 dev_server.py（env 从 server/.env 注入，覆盖 GO_SERVER_URL=http://127.0.0.1:8080）；4) e2e：python -X utf8 tests/test_e2e.py（6 问全 OK）。

## 相关
`mem:core`、`mem:conventions`