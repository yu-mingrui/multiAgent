from Director import graph
import uuid
config = {"configurable": {"thread_id": str(uuid.uuid4())}}
#测试director
query="请给我讲一个郭德纲的笑话"
res = graph.invoke({"messages":[query]},config, stream_mode="values")
print(res["messages"][-1].content)