from fastapi import FastAPI
from pydantic import BaseModel

import sys
import uuid
from pathlib import Path


from Director import graph
import uvicorn

app = FastAPI()

class DirectorRequest(BaseModel):
    query: str

@app.post("/api/director")
def director(request: DirectorRequest):
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    state = {"messages": [request.query]}
    result = graph.invoke(state, config, stream_mode="values")

    response_text = None
    if isinstance(result, dict) and "messages" in result and result["messages"]:
        last_message = result["messages"][-1]
        response_text = getattr(last_message, "content", str(last_message))

    return {"query": request.query, "response": response_text}

#curl -X POST "http://127.0.0.1:8001/api/director" -H "Content-Type: application/json" -d "{\"query\":\"做一个从康桥玥棠去二七万达的地规划路线\"}"

if __name__ == "__main__":
    # Use import string so the reloader can import the app module correctly
    uvicorn.run("api:app", port=8001, reload=True)