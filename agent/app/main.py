"""FastAPI 入口：以 ndjson 流式吐出 Agent 事件。

事件类型：
- token   : {"event":"token","data":{"delta":"..."}}          仅来自回答生成节点
- card    : {"event":"card","data":{"type":..,"payload":..}}   结构化卡片
- table   : {"event":"table","data":{"type":..,"payload":..}}  结构化表格
- sources : RAG 引用来源，{"docs":[{title, chunk_id, score}]}
- tool_start / tool_end : 工具调用进度（进程内工具，含缓存命中）
- error   : {"event":"error","data":{"message":"..."}}
- done    : {"event":"done","data":{"thread_id":..,"usage":{..}}}

会话历史接口（供小程序「历史对话」抽屉，经 Go 网关转发）：
- GET    /sessions?user_id=                 会话列表（标题 + 最近活跃）
- POST   /sessions                          注册新对话占位 / 每轮 run upsert
- GET    /sessions/{sid}/messages?user_id=  恢复某会话的纯文本消息
- DELETE /sessions/{sid}?user_id=           删除会话（注册行 + checkpoints + 摘要）
所有接口都校验 thread_id 归属（default:user:session），防越权访问他人会话。

"""
import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from core.checkpointer import close_checkpointer
from core.graph import get_graph
from core.pg import close_pool as close_app_pool
from core.redis import close_redis
from core.sessions import (
    delete_session,
    ensure_sessions_table,
    list_sessions,
    session_messages,
    thread_id_of,
    upsert_session,
    valid_session_id,
)
from core.state import initial_state
from db import close_pool as close_ro_pool
from nodes import log_agent_run
from tools import close_http_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stocka.agent")


def _check_ownership(user_id: str, session_id: str) -> str:
    """校验 session_id 合法性并拼出 thread_id，非法一律 400。

    Go 网关的 user_id 来自 JWT（不可伪造），这里再补一层防御：
    thread_id 必须等于 default:{user_id}:{session_id}，从结构上杜绝
    用他人 session_id 读写别人会话的可能。
    """
    uid = (user_id or "").strip()
    if not uid or len(uid) > 64 or not valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="invalid user_id or session_id")
    return thread_id_of(uid, session_id)


async def _checkpoint_cleanup_loop() -> None:
    """后台回收孤儿 checkpoint：启动 60s 后跑一次，之后按间隔循环。

    durability="exit" 已把每轮 run 的行数压到 1 份，但 benchmark / 调试线程
    不在 chat_sessions 里、也没人删，行数会随跑批累积。这里只清理这类线程，
    用户会话仍由 SESSION_KEEP_MAX 与手动删除管理。
    """
    from config import CHECKPOINT_CLEANUP_INTERVAL_HOURS
    from core.sessions import prune_orphan_threads

    # 启动后稍等，避开建池/预热与首个请求的高峰
    await asyncio.sleep(60)
    while True:
        try:
            removed = await prune_orphan_threads()
            logger.info("checkpoint cleanup: removed %d orphan threads", removed)
        except Exception as e:  # noqa: BLE001 - 后台任务失败不影响服务
            logger.warning("checkpoint cleanup failed: %s", e)
        if CHECKPOINT_CLEANUP_INTERVAL_HOURS <= 0:
            return
        await asyncio.sleep(CHECKPOINT_CLEANUP_INTERVAL_HOURS * 3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 预热图与 checkpointer（建表 + 连接池）
    try:
        await get_graph()
    except Exception as e:  # noqa: BLE001 - 预热失败不阻断启动，首个请求会再试
        logger.warning("graph warmup failed: %s", e)
    # 预热 tiktoken：首次 encode 要加载/下载词表（实测约 3.9s），
    # 不预热会把它算到首个请求的 guardrail 上，TTFT 直接多 4s
    try:
        from core.llm import estimate_tokens

        estimate_tokens("预热")
    except Exception as e:  # noqa: BLE001 - 预热失败不阻断启动
        logger.warning("tokenizer warmup failed: %s", e)
    # 会话注册表（chat_sessions）：历史对话列表的存储，幂等建表
    try:
        await ensure_sessions_table()
    except Exception as e:  # noqa: BLE001 - 建表失败不阻断启动，接口侧会懒建重试
        logger.warning("ensure chat_sessions failed: %s", e)
    # 预热工具集：12 个 @tool 进程内构造（不触发 akshare 导入），
    # 留到首请求会白等几十毫秒
    try:
        from subgraphs.stock import warm_tools

        logger.info("stock tools warmed: %d", await warm_tools())
    except Exception as e:  # noqa: BLE001 - 预热失败不阻断启动
        logger.warning("tool warmup failed: %s", e)
    # 后台清理孤儿 checkpoint（benchmark/调试线程）；失败不影响主流程
    cleanup_task = asyncio.create_task(_checkpoint_cleanup_loop())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await close_checkpointer()
    # 主数据只读池 / 应用写池 / Redis：与 checkpointer 分开收尾，任一失败不影响其它
    try:
        await close_ro_pool()
    except Exception as e:  # noqa: BLE001 - 收尾失败不影响进程退出
        logger.warning("close read-only pool failed: %s", e)
    try:
        await close_app_pool()
    except Exception as e:  # noqa: BLE001
        logger.warning("close app pool failed: %s", e)
    try:
        await close_redis()
    except Exception as e:  # noqa: BLE001
        logger.warning("close redis failed: %s", e)
    await close_http_client()
    # 重排用的共享客户端
    try:
        from rag.reranker import close_rerank_client

        await close_rerank_client()
    except Exception:  # noqa: BLE001 - 收尾失败不影响进程退出
        pass


app = FastAPI(title="StockA Agent", lifespan=lifespan)


class RunRequest(BaseModel):
    # 长度约束是第二道闸：Go 网关已按 2000 字拦一次，直连 agent 的调试请求也不能无界
    thread_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=2000)
    user_id: str = Field(min_length=1, max_length=64)


def _line(event: str, data: dict) -> str:
    """序列化成一行 ndjson。"""
    return json.dumps({"event": event, "data": data}, ensure_ascii=False) + "\n"


# 用户真正“看到答案”的事件：token / card / table。
# tool_start、tool_end、sources 只是过程提示，不能算 TTFT，否则审计里的首响
# 会明显早于用户真实看到文字/卡片的时间（股票路径尤其失真）。
_ANSWER_EVENTS = frozenset({"token", "card", "table"})


# 后台审计任务引用：不持有引用的话任务可能在跑完前被 GC 回收
_bg_tasks: set = set()


def _is_answer_event(event: str) -> bool:
    return event in _ANSWER_EVENTS


async def stream_events(
    thread_id: str, query: str, user_id: str
) -> AsyncGenerator[dict, None]:
    """产出 {"event":..., "data":...} 事件。

    合并进网关后由网关直接转 SSE 帧，省掉一层 ndjson 序列化；
    事件名与 data 结构一字不改，前端解析逻辑无需变动。
    """
    usage: dict = {}
    t0 = time.monotonic()
    ttft_ms = None
    first_event_ms = None
    timings: dict = {}
    error_code = None
    graph = None
    # 审计字段随流收集：正常结束时不再为了它们整份回读 state
    intent = None
    confidence = None
    tool_calls = None
    # recursion_limit 显式放宽：主图 8 个节点 + stock 子图最多 3 轮 × 2 节点，
    # 默认 25 在「多跳取数 + 子图事件冒泡」下余量太薄，触顶会直接抛 GraphRecursionError
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
    try:
        graph = await get_graph()
        # 不在请求路径裁剪 checkpoint：prune_thread_checkpoints 会 alist + 删除 + aput，
        # 实测多轮会话第二轮会在 prune 阶段把 uvicorn 内存推到 2G+ 并卡住 60~80s。
        # 改为 durability="exit"：每轮只在图结束时落一份 checkpoint，行数不再按 superstep 增长；
        # 历史清理交给会话删除或后续离线维护任务。
        inp = initial_state(user_id=user_id, thread_id=thread_id, query=query)
        inp["messages"] = [HumanMessage(content=query)]

        # 只订阅 custom（token/card/tool 事件）与 updates（审计字段）：
        # "messages" 模式会把每个 LLM token 再经 channel 抛一遍，而这里从不消费它
        #（token 由生成节点显式 emit 成 custom 事件），大回答下白扛几千次事件
        async for item in graph.astream(
            inp, config=config,
            stream_mode=["custom", "updates"],
            subgraphs=True,
            durability="exit",
        ):
            # subgraphs=True 时 item 是 (namespace, mode, data) 三元组：只有带上它，
            # stock/rag/chat 子图内部的 token 与 card/table/tool 事件才会冒泡到父流，
            # 否则子图被当成单个节点、内部事件全丢（前端只收到 done）；
            # 同时兼容不含命名空间的 (mode, data) 二元组与单模式裸 data
            if isinstance(item, tuple) and len(item) == 3:
                _ns, mode, data = item
            elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
                mode, data = item
            else:  # 兜底：单模式
                mode, data = "messages", item

            # token 由生成节点（chat/rag/stock/refuse/clarify）在 astream 循环里
            # 显式 emit("token", ...) 经 custom 模式透传，避免与 messages 模式重复
            if mode == "custom":
                # data 已是 {"event":..,"data":..}（由 subgraphs.emit 产出），直接透传
                if isinstance(data, dict) and "event" in data:
                    event = str(data.get("event") or "")
                    if first_event_ms is None:
                        first_event_ms = int((time.monotonic() - t0) * 1000)
                    # 只有 token/card/table 才算用户看到答案的 TTFT；
                    # tool_start / tool_end / sources 是过程提示，单独记 first_event_ms
                    if ttft_ms is None and _is_answer_event(event):
                        ttft_ms = int((time.monotonic() - t0) * 1000)
                    yield data

            elif mode == "updates":
                if isinstance(data, dict):
                    post_update = data.get("post")
                    if isinstance(post_update, dict) and post_update.get("usage"):
                        usage = post_update["usage"]
                    for node_update in data.values():
                        if not isinstance(node_update, dict):
                            continue
                        # 阶段计时随 updates 增量收集；子图节点也会冒泡到这里
                        if isinstance(node_update.get("timings"), dict):
                            timings.update(node_update["timings"])
                        # 护栏/分类节点写入的 error 作为审计错误码
                        if node_update.get("error"):
                            error_code = str(node_update["error"])[:100]
                        # intent/confidence 由 classify 写、rag 未命中时覆写为 stock_agent；
                        # tool_results 是 operator.add 累加，按增量累加即得总数
                        if node_update.get("intent"):
                            intent = node_update["intent"]
                        if node_update.get("confidence") is not None:
                            confidence = node_update["confidence"]
                        added = node_update.get("tool_results")
                        if isinstance(added, list):
                            tool_calls = (tool_calls or 0) + len(added)

    except Exception as e:  # noqa: BLE001 - 任何异常都转成 error 事件，不让流崩掉
        logger.exception("event_generator failed")
        error_code = type(e).__name__
        if first_event_ms is None:
            first_event_ms = int((time.monotonic() - t0) * 1000)
        yield {"event": "error", "data": {"message": f"{type(e).__name__}: {e}"}}

    finally:
        total_ms = int((time.monotonic() - t0) * 1000)
        # 审计落库：字段优先用流里收集到的（省掉一次全量 state 回读），
        # 只有流中途失败、没收集到 intent 时才回读兜底。
        # 这里只做兜底取数，落库本身挪到 done 之后。
        try:
            if graph is not None and (intent is None or not timings):
                snapshot = await graph.aget_state(config)
                values = (snapshot.values if snapshot else None) or {}
                intent = intent or values.get("intent") or None
                confidence = values.get("confidence") if confidence is None else confidence
                if tool_calls is None:
                    tool_calls = len(values.get("tool_results") or [])
                if not usage and isinstance(values.get("usage"), dict):
                    usage = values["usage"]
                if isinstance(values.get("timings"), dict):
                    timings = {**timings, **values["timings"]}
        except Exception:  # noqa: BLE001
            logger.warning("agent run state snapshot failed", exc_info=True)

        # 先发 done：客户端不该等一条 INSERT。非数字 user_id 置空，避免 BIGINT 插入报错
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            uid = None
        yield {"event": "done", "data": {"thread_id": thread_id, "usage": usage}}

        task = asyncio.create_task(
            log_agent_run(
                user_id=uid,
                thread_id=thread_id,
                intent=intent,
                confidence=confidence,
                tokens_in=usage.get("input_tokens", 0),
                tokens_out=usage.get("output_tokens", 0),
                ttft_ms=ttft_ms,
                first_event_ms=first_event_ms,
                total_ms=total_ms,
                tool_calls=tool_calls,
                error_code=error_code,
                timings=timings,
            )
        )
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


async def event_generator(
    thread_id: str, query: str, user_id: str
) -> AsyncGenerator[str, None]:
    """ndjson 版本，保留给独立部署/调试时的 POST /run/stream。"""
    async for event in stream_events(thread_id, query, user_id):
        yield _line(str(event.get("event") or ""), event.get("data") or {})


@app.post("/run/stream")
async def run_stream(req: RunRequest):
    return StreamingResponse(
        event_generator(req.thread_id, req.query, req.user_id),
        media_type="application/x-ndjson",
    )


@app.get("/healthz")
async def health():
    return {"status": "ok"}


# ===== 会话历史（历史对话抽屉）=====

class SessionUpsert(BaseModel):
    # user_id / session_id 的合法性统一由 _check_ownership 判（回 400 带原因）；
    # 放进 Pydantic 会变成 422，丢掉「太长/非法字符」这层语义
    user_id: str
    session_id: str
    title: str = Field(default="", max_length=200)


@app.get("/sessions")
async def get_sessions(user_id: str):
    """会话列表：按最近活跃倒序；user_id 由 Go 网关从 JWT 解析后注入。"""
    if not (user_id or "").strip():
        raise HTTPException(status_code=400, detail="invalid user_id")
    return {"sessions": await list_sessions(user_id.strip())}


@app.post("/sessions")
async def post_session(body: SessionUpsert):
    """注册/刷新会话。

    两种调用方：
    - 「新对话」按钮：title 为空 → 占位标题「新对话」，auto_title=true，
      首条真实消息到达后标题被替换为首个问题；
    - 每轮 run（Go 网关代理时带 query）：同一条会话只更新 updated_at，
      已有真实标题不被覆盖。
    """
    _check_ownership(body.user_id, body.session_id)
    await upsert_session(
        body.user_id.strip(), body.session_id, body.title, auto_title=not body.title.strip()
    )
    return {"ok": True}


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, user_id: str):
    """某会话的纯文本消息（切会话/恢复上下文）。"""
    _check_ownership(user_id, session_id)
    return {"messages": await session_messages(user_id.strip(), session_id)}


@app.delete("/sessions/{session_id}")
async def remove_session(session_id: str, user_id: str):
    """删除会话：注册行 + checkpoints + Redis 滚动摘要。"""
    _check_ownership(user_id, session_id)
    deleted = await delete_session(user_id.strip(), session_id)
    return {"ok": True, "deleted": deleted}
