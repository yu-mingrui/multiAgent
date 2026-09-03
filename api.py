from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import uuid

from Director import graph
import uvicorn

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DirectorRequest(BaseModel):
    query: str
    user_id: str | None = None
    session_id: str | None = None


def build_thread_id(user_id: str | None, session_id: str | None) -> str:
    if user_id and session_id:
        return f"user:{user_id}:session:{session_id}"
    if user_id:
        return f"user:{user_id}"
    if session_id:
        return f"session:{session_id}"
    return str(uuid.uuid4())


@app.post("/api/director")
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

    try:
        result = graph.invoke(state, config, stream_mode="values")
    except Exception as exc:
        error_message = str(exc)
        return {
            "query": request.query,
            "response": f"模型调用失败：{error_message}",
            "user_id": user_id,
            "session_id": session_id,
            "thread_id": thread_id,
            "error": "model_request_failed",
        }
    print(">>>>result", result)
    response_text = None
    last_message = result["messages"][-1]
    response_text = getattr(last_message, "content") 

    return {
        "query": request.query,
        "response": response_text,
        "user_id": user_id,
        "session_id": session_id,
        "thread_id": thread_id,
    }


if __name__ == "__main__":
    # Use import string so the reloader can import the app module correctly
    uvicorn.run("api:app", port=8001, reload=True)