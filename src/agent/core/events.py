"""问答事件流：把 LangGraph 的运行过程转成 {"event":..,"data":..} 事件。

进程内由 `server/agent_bridge.py` 直接消费并转 SSE 帧，不再走 ndjson。
事件类型：
- token   : {"delta": "..."}                     仅来自回答生成节点
- card    : {"type":..,"payload":..}              结构化卡片
- table   : {"type":..,"payload":..}              结构化表格
- sources : {"docs":[{title, chunk_id, score}]}   RAG 引用来源
- tool_start / tool_end : 工具调用进度（进程内工具，含缓存命中）
- error   : {"message": "..."}
- done    : {"thread_id":..,"usage":{..}}
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator

from langchain_core.messages import HumanMessage

from agent.core.graph import get_graph
from agent.core.state import initial_state
from agent.nodes import log_agent_run

logger = logging.getLogger("stocka.agent")

# 用户真正"看到答案"的事件：token / card / table。
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
            inp,
            config=config,
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
