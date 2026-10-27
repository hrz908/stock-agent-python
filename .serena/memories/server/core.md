# src/gateway/（FastAPI 单进程网关）

FastAPI + psycopg3 + redis.asyncio + PyJWT。入口 `gateway.main:app`，与 agent 同进程。
接口总表与契约细节见 `docs/API.md`。

## 目录要点
- `main.py` 装配：lifespan（建池 / 建表 / 预热 agent）、异常→信封、路由三档（公开 / 已鉴权 / 行情限流）
- `api/v1/`：路由层，一个业务域一个文件（auth / user / watchlist / alert / stock / chat / quickask）
- `services/`：业务编排；`repositories/`：全部 SQL；`adapters/`：quote / wechat / wecom / agent_bridge
- `infra/`：config、resp、deps、context、web、hook、collections、cache、agent_limiter、db/
- `middleware/`：gateway（纯 ASGI，防 SSE 被缓冲）、rate_limit（滑动窗口 Lua）、guards、clientip
- `security/`：jwt（HS256，拒算法混淆）、auth（user_id 只从 Bearer 解出）
- `domain/`：amount（金额定点格式化）、stockcode（代码归一化）

## 关键行为
- 接口前缀 `/api/v1`；`/healthz` 不鉴权不限流；401 触发小程序静默重登
- 限流 Redis 故障 fail-open；对话三道闸门（QPS / 并发 / 日配额）
- 默认 DSN `postgres://postgres:postgres@localhost:5432/stock_agent?sslmode=disable`，Redis `127.0.0.1:6379`
- 网关自管三张表的 DDL 在 `sql/bootstrap.sql`，启动时幂等执行
- ASR：`/api/v1/asr/transcribe` 走 SiliconFlow 的 SenseVoiceSmall，密钥回退 `OPENAI_API_KEY`

## 相关
`mem:core`、`mem:conventions`、`mem:suggested_commands`
