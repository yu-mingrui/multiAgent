#主文件
import sys
from pathlib import Path

import local_setting

from langgraph.graph import StateGraph,MessagesState,START,END
from typing import Annotated,TypedDict,Literal
from langgraph.types import Command,interrupt
from operator import add
from langchain_core.messages import AnyMessage,AIMessage,HumanMessage
from langgraph.checkpoint.redis import RedisSaver
from langgraph.config import get_stream_writer
from langchain_mcp_adapters.client import MultiServerMCPClient
import asyncio
import traceback
from langchain.agents import create_agent
from langchain_core.prompts import ChatPromptTemplate
import os
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_redis import RedisVectorStore,RedisConfig
import redis

from dotenv import load_dotenv
import os

load_dotenv()

nodes = ['supervisor','travel','couplet','joke','other']
class state(TypedDict):
    messages: Annotated[list[AnyMessage], add]
    type: str


def _as_human_messages(messages):
    normalized = []
    for item in messages or []:
        if isinstance(item, HumanMessage):
            normalized.append(item)
        elif isinstance(item, str):
            normalized.append(HumanMessage(content=item))
        elif hasattr(item, "content") and isinstance(item.content, str):
            normalized.append(HumanMessage(content=item.content))
    return normalized


def supervisor_node(state: state):
    print(">>>>supervisor_node")
    writer = get_stream_writer()
    writer({"node": "supervisor_node"})

    latest_message = (state.get("messages") or [None])[-1]
    if isinstance(latest_message, AIMessage) and state.get("type") in nodes:
        writer({"supervisor_type": "本轮业务处理已完成"})
        return {"type": END}

    user_messages = _as_human_messages(state.get("messages", []))
    # print("user_messages", user_messages)
    if not user_messages:
        writer({"supervisor_type": "当前轮次没有发现新的用户消息"})
        return {"type": END}

    latest_question = user_messages[-1].content
    prompt = '''你是一个客服助手，负责对用户问题进行分类，并将任务分配给其它agent执行
     如果用户的问题和旅游路线规划相关，那就返回travel
     如果用户的问题是希望讲一个笑话，那就返回joke
     如果用户问题是对一个对联，那就返回couplet
     如果是其它问题，那就返回other
     除了这几个选项外不要返回其它内容
     
    '''
    prompts = [
        {
            "role": "system",
            "content": prompt,
        },
        {
            "role": "user",
            "content": latest_question,
        },
    ]
    print(">>>prompts", prompts)
    response = local_setting.qwen.invoke(prompts)
    print('>>>>>', response)
    selected_type = response.content.strip()
    writer({"supervisor_type": f"问题分类结果是{selected_type}"})
    if selected_type in nodes:
        return {"type": selected_type}
    raise ValueError("无法识别问题类型")

def travel_node(state: state):
    print(">>>>travel_node")
    writer = get_stream_writer()
    writer({"node": "travel_node"})

    raw_messages = state.get("messages", [])
    latest_user_content = None
    if raw_messages:
        last_item = raw_messages[-1]
        if isinstance(last_item, HumanMessage):
            latest_user_content = last_item.content
        elif isinstance(last_item, str):
            latest_user_content = last_item
        elif hasattr(last_item, "content") and isinstance(last_item.content, str):
            latest_user_content = last_item.content

    #高德地图mcp的配置信息
    async def async_travel():
        client = MultiServerMCPClient(
            {
                "amap-maps": {
                    "transport": "stdio",
                    "command": "npx.cmd",
                    "args": ["-y", "@amap/amap-maps-mcp-server"],
                    "env": {
                        "AMAP_MAPS_API_KEY": os.getenv("AMAP_MAPS_API_KEY")
                    },
                }
            }
        )
        systemPrompt = '''你是一个专业的旅行规划助手，你需要根据用户的输入，生成一份路线规划。请用中文回答，并返回一个不超过100字的规划结果'''
        prompts =[
            {
                "role": "system",
                "content": systemPrompt,
            },
            {
                "role": "user",
                "content": latest_user_content or "",
            },
        ]
        print(">>>>travel prompts", prompts)
        tools = await client.get_tools()
        # print(">>>>travel tools", tools)
        agent = create_agent(
            model=local_setting.qwen,
            tools=tools,
            checkpointer=False,
        )
        result = await agent.ainvoke({"messages": prompts})
        print(">>>>travel result", result)
        return result
    
    try:
        result = asyncio.run(async_travel())
        writer({"travel_result": result["messages"][-1].content})
        return {"messages": [AIMessage(result["messages"][-1].content)], "type": 'travel'}
    except Exception as e:
        print(f"Travel node error: {e}")
        writer({"travel_error": str(e)})
        return {"messages": [AIMessage(f"路线规划失败：{str(e)}")], "type": 'other'}

def couple_node(state: state):
    print(">>>>couple_node")
    writer = get_stream_writer()
    writer({"node": "couple_node"})
    prompt_template = ChatPromptTemplate.from_messages([
    ("system", "你是一个专业的对联大师，你的任务是根据用户给出的上联，设计一下下联\n    回答时可以参考下面的参考对联。\n     参考对联：{samples}\n请用中文回答,回答不超过30字"),
    ("user", "上联是：{text}")
])
    query = state["messages"][-1].content if isinstance(state["messages"][-1], HumanMessage) else state["messages"][-1]
    if isinstance(state["messages"][-1], str):
        query = state["messages"][-1]
    embedding_model = DashScopeEmbeddings(
            model="qwen3.7-text-embedding-flash",
            dashscope_api_key= os.getenv("DASHSCOPE_API_KEY"),
    )
    redis_url = os.getenv("REDIS_URL")
    redis_client = redis.from_url(redis_url)
    print('redis连接测试:', redis_client.ping()) #测试连接，返回true,表示连接成功
    config = RedisConfig(
        index_name="couplet",
        redis_url=redis_url,
    )
    redisstore= RedisVectorStore(
        embeddings=embedding_model,
        config=config,
    )
    scord_results = redisstore.similarity_search_with_score(query, k=5)
    print('scord_results',scord_results)
    samples = []
    for doc, score in scord_results: 
        print(doc, score)
        samples.append(doc.page_content)
    prompt = prompt_template.invoke({"text": query, "samples": samples})
    response = local_setting.qwen.invoke(prompt)
    writer({">>>>couplet_result": response.content})
    return {"messages": [AIMessage(response.content)],"type":"couplet"}

def other_node(state: state):
    print(">>>>other_node")
    writer = get_stream_writer()
    writer({"node": "other_node"})
    return {"messages": [AIMessage("无法回答您的问题")]}

def joke_node(state: state):
    print(">>>>joke_node")
    writer = get_stream_writer()
    writer({"node": "joke_node"})
    systemPrompt = '''你是一个笑话大师，根据用户的问题，写一个不超过20字的笑话
    '''
    query = state["messages"][-1].content if isinstance(state["messages"][-1], HumanMessage) else state["messages"][-1]
    if isinstance(state["messages"][-1], str):
        query = state["messages"][-1]
    prompts =[
        {
            "role": "system",
            "content": systemPrompt,
        },
        {
            "role": "user",
            "content": query,
        },
        
    ]
    response = local_setting.qwen.invoke(prompts)
    return {"messages": [AIMessage(response.content)],"type":"joke"}
#条件路由
def routing_func(state: state):
    print(">>>>routing_func")
    writer = get_stream_writer()
    writer({"node": "routing_func"})
    if state["type"] == "joke":
        return "joke_node"
    elif state["type"] == "travel":
        return "travel_node"
    elif state["type"] == "couplet":
        return "couple_node"
    elif state["type"] == END:
        return END
    else:
        return "other_node"
    
#构建图
builder = StateGraph(state)
builder.add_node(supervisor_node)
builder.add_node(travel_node)
builder.add_node(couple_node)
builder.add_node(joke_node)
builder.add_node(other_node)
builder.add_edge(START,'supervisor_node')
#添加条件边
builder.add_conditional_edges('supervisor_node',routing_func,[ "travel_node", "couple_node", "joke_node", "other_node",END
])
builder.add_edge("travel_node", "supervisor_node")
builder.add_edge("couple_node", "supervisor_node")
builder.add_edge("joke_node", "supervisor_node")
builder.add_edge("other_node", "supervisor_node")
redis_url = os.getenv("REDIS_URL")
checkpointer = RedisSaver(redis_url=redis_url)
checkpointer.setup()
#编译图
graph = builder.compile(checkpointer=checkpointer)

import uuid
if __name__ == "__main__":
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    for chunk in graph.stream({"messages": ["上联：春江花月夜"]},config,stream_mode="custom"):
        print(chunk)

    # res = graph.invoke({"messages": ["今天天气怎么样？"]},config,stream_mode="values")
    # print(res["messages"][-1].content)