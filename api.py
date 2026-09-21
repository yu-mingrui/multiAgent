from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import json
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
import uuid
import os

from Director import checkpointer, graph
import uvicorn
import redis.asyncio as aioredis

session_store = None


# 管理应用启动和关闭时的 Redis 资源与检查点生命周期。
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行：初始化会话索引 Redis 客户端，并准备图检查点存储。
    global session_store
    session_store = aioredis.from_url(os.getenv("REDIS_URL"), decode_responses=True)
    await checkpointer.setup()
    try:
        yield
    finally:
        # 关闭应用时释放 Redis 连接，避免连接泄露。
        await session_store.aclose()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DirectorRequest(BaseModel):
    # 描述一次对话请求及其可选的用户和会话身份。
    query: str
    user_id: str | None = None
    session_id: str | None = None


# 根据用户和会话信息组装稳定的 LangGraph thread_id，保证同一用户的同一会话能恢复上下文。
def build_thread_id(user_id: str | None, session_id: str | None) -> str:
    if user_id and session_id:
        return f"user:{user_id}:session:{session_id}"
    if user_id:
        return f"user:{user_id}"
    if session_id:
        return f"session:{session_id}"
    return str(uuid.uuid4())


# 将事件名称和数据对象编码成 SSE 传输格式。
def sse_message(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"

# 从流式 chunk 中提取当前轮次新增的 AI 回复，供前端逐字展示。
def extract_latest_answer(chunk, start_index: int) -> str | None:
    if not isinstance(chunk, dict):
        return None

    messages = chunk.get("messages") or []
    for message in reversed(messages[start_index:]):
        if message.__class__.__name__ != "AIMessage":
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
    return None


# 生成当前用户在 Redis 中的会话索引键，用于保存会话列表与详情。
def session_index_key(user_id: str) -> str:
    return f"director:sessions:{user_id}"

# 将 LangChain 消息对象转换成前端展示的历史记录结构。
def message_to_history_item(message):
    content = getattr(message, "content", message)
    if not isinstance(content, str):
        return None
    role = "assistant" if message.__class__.__name__ == "AIMessage" else "user"
    return {"role": role, "content": content}


# 保存或更新会话的标题、预览和更新时间，用于右侧历史会话列表。
async def save_session_metadata(user_id: str, session_id: str, title: str, preview: str = ""):
    if session_store is None:
        return
    existing = await session_store.hget(session_index_key(user_id), session_id)
    if existing:
        title = json.loads(existing).get("title") or title
    metadata = {
        "session_id": session_id,
        "title": title[:80],
        "preview": preview[:120],
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    await session_store.hset(session_index_key(user_id), session_id, json.dumps(metadata, ensure_ascii=False))


@app.get("/api/sessions")
# 返回指定用户的所有历史会话列表，按更新时间倒序排列。
async def list_sessions(user_id: str):
    if not user_id or session_store is None:
        return {"sessions": []}
    records = await session_store.hvals(session_index_key(user_id))
    sessions = [json.loads(record) for record in records]
    sessions.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return {"sessions": sessions}


@app.get("/api/sessions/{session_id}")
# 根据会话 ID 恢复 checkpoint 中的完整消息历史，返回前端可渲染的会话记录。
async def get_session(session_id: str, user_id: str):
    if not user_id:
        return {"session_id": session_id, "messages": []}
    config = {"configurable": {"thread_id": build_thread_id(user_id, session_id)}}
    checkpoint = await graph.aget_state(config)
    messages = [message_to_history_item(message) for message in checkpoint.values.get("messages", [])]
    return {"session_id": session_id, "messages": [message for message in messages if message]}

@app.delete("/api/sessions/{session_id}")
# 删除指定会话的 checkpoint 和 Redis 会话索引，确保历史清理一致。
async def delete_session(session_id: str, user_id: str):
    if not user_id:
        return {"deleted": False}
    thread_id = build_thread_id(user_id, session_id)
    await checkpointer.adelete_thread(thread_id)
    if session_store is not None:
        await session_store.hdel(session_index_key(user_id), session_id)
    return {"deleted": True, "session_id": session_id}

@app.post("/api/director")
# 接收用户请求，并以 SSE 方式持续流出回答，支持前端逐字显示和链路恢复。
def director(
    request: DirectorRequest,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
):
    user_id = x_user_id or request.user_id
    session_id = x_session_id or request.session_id

    thread_id = build_thread_id(user_id, session_id)
    config = {"configurable": {"thread_id": thread_id}}
    state = {"messages": [request.query]}

    async def event_stream():
        try:
            # 先保存当前会话的元数据，再读取上一个 checkpoint 以定位增量回答。
            await save_session_metadata(user_id or "anonymous", session_id or thread_id, request.query)
            checkpoint = await graph.aget_state(config)
            previous_messages = checkpoint.values.get("messages", [])
            start_index = len(previous_messages)
            latest_answer = ""
            async for stream_item in graph.astream(state, config, stream_mode="values"):
                # 取本轮新增的 AI 回复，避免重复返回历史内容。
                answer = extract_latest_answer(stream_item, start_index)

                if answer and answer != latest_answer:
                    delta = answer[len(latest_answer):] if answer.startswith(latest_answer) else answer
                    latest_answer = answer
                    yield sse_message("answer", {"delta": delta})

            if user_id and session_id:
                await save_session_metadata(user_id, session_id, request.query, latest_answer)

            yield sse_message("done", {})
        except Exception as exc:
            print(f"Director stream error: {exc!r}")
            traceback.print_exc()
            yield sse_message("error", {
                "error": "model_request_failed",
                "message": "模型暂时无法回答，请稍后重试。",
            })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive","X-Accel-Buffering": "no",},
    )


app.mount(
    "/frontend",
    StaticFiles(directory=Path(__file__).parent / "frontend", html=True),
    name="frontend",
)


if __name__ == "__main__":
    # Use import string so the reloader can import the app module correctly
    uvicorn.run("api:app", port=8001, reload=True)