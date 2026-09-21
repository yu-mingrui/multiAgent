#主文件
import sys
import re
from pathlib import Path

import local_setting

from langgraph.graph import StateGraph,MessagesState,START,END
from typing import Annotated,NotRequired,TypedDict,Literal
from langgraph.types import Command,interrupt
from operator import add
from langchain_core.messages import AnyMessage,AIMessage,HumanMessage
from langgraph.checkpoint.redis import AsyncRedisSaver
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
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv
import os

load_dotenv()

nodes = ['supervisor','travel','couplet','joke','other']
class state(TypedDict):
    messages: Annotated[list[AnyMessage], add]
    type: NotRequired[str]
    conversation_summary: NotRequired[str]

# 将用户消息统一转换成 HumanMessage，方便后续执行路由和节点逻辑。
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

# 生成最近一段对话文本，用于路由和上下文补充。
def conversation_context(messages, limit: int = 12) -> str:
    context = []
    for message in messages[-limit:]:
        if isinstance(message, HumanMessage):
            role, content = "用户", message.content
        elif isinstance(message, AIMessage):
            role, content = "助手", message.content
        elif isinstance(message, str):
            role, content = "用户", message
        else:
            continue
        if isinstance(content, str):
            context.append(f"{role}：{content}")
    return "\n".join(context)

# 从当前状态中整理出历史摘要和最近对话，供 supervisor 和各节点引用。
def memory_context(current_state: state, exclude_latest: bool = False) -> str:
    summary = current_state.get("conversation_summary", "")
    messages = current_state.get("messages", [])
    if exclude_latest and messages:
        # 去掉最后一条，避免当前消息被重复计入历史上下文。
        messages = messages[:-1]
    history = conversation_context(messages)
    sections = []
    if summary:
        sections.append(f"历史摘要：\n{summary}")
    if history:
        sections.append(f"最近对话：\n{history}")
    return "\n\n".join(sections) or "暂无历史对话"

# 把用户的千奇百怪提问还原成真正的上联文本，再用于向量检索。
def normalize_couplet_query(raw_text: str) -> str:
    if not isinstance(raw_text, str):
        return ""
    text = raw_text.strip()
    text = text.replace("上联：", "").replace("下联：", "")
    text = text.replace("的下联", "").replace("的上联", "")
    text = re.sub(r"^(?:请|帮我|给我|来个|来一|写一个|生成|出一个|对一个|对个|对一下|对)\s*", "", text)
    text = re.sub(r"(?:上联|下联)\s*$", "", text)
    text = re.sub(r"[，。！？、；；\n\r\t\s]+$", "", text)
    text = text.strip("：： ")
    return text

# 维护会话摘要：当对话过长时，压缩旧对话并保留最近的关键信息。
def update_conversation_summary(current_state: state) -> str | None:
    messages = current_state.get("messages", [])
    keep_recent = 6
    if len(messages) <= 12:
        return current_state.get("conversation_summary")

    older_context = conversation_context(messages[:-keep_recent])
    existing_summary = current_state.get("conversation_summary", "")
    prompt = [
        {
            "role": "system",
            "content": "你负责维护客服会话摘要。只保留对后续回答有用的事实、用户意图、已确认地点、偏好和未完成事项。不要编造信息，不要输出分析过程，使用简洁中文。",
        },
        {
            "role": "user",
            "content": (
                f"已有摘要：\n{existing_summary or '无'}\n\n"
                f"需要压缩的较早对话：\n{older_context}"
            ),
        },
    ]
    response = local_setting.qwen.invoke(prompt)
    summary = getattr(response, "content", "")
    return summary.strip() or existing_summary

# 负责判断当前用户消息应该交给哪个业务节点处理。
def supervisor_node(state: state):
    print(">>>>supervisor_node")
    writer = get_stream_writer()
    writer({"node": "supervisor_node"})

    latest_message = (state.get("messages") or [None])[-1]
    if isinstance(latest_message, AIMessage) and state.get("type") in nodes:
        writer({"supervisor_type": "本轮业务处理已完成"})
        summary = update_conversation_summary(state)
        result = {"type": END}
        if summary:
            result["conversation_summary"] = summary
        return result

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
    你必须结合完整对话历史理解当前问题。当前问题可能是对上一轮的追问、修改或否定，不能只看当前这一句。
     除了这几个选项外不要返回其它内容
     
    '''
    prompts = [
        {
            "role": "system",
            "content": prompt,
        },
        {
            "role": "user",
            "content": f"{memory_context(state, exclude_latest=True)}\n\n当前用户消息：\n{latest_question}",
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

# 旅游节点：调用地图 MCP 工具和旅行规划 Agent，生成路线建议。
async def travel_node(state: state):
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

    travel_request = (
        f"{memory_context(state, exclude_latest=True)}\n\n"
        f"本轮用户问题：{latest_user_content or '请根据上下文继续处理'}"
    )

    # 高德地图 MCP 的配置信息。
    try:
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
                "content": travel_request,
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

        writer({"travel_result": result["messages"][-1].content})
        return {"messages": [AIMessage(result["messages"][-1].content)], "type": 'travel'}
    except Exception as e:
        print(f"Travel node error: {e}")
        writer({"travel_error": str(e)})
        return {"messages": [AIMessage(f"路线规划失败：{str(e)}")], "type": 'other'}

# 对联节点：用 Redis 向量检索相似对联样例，再交给模型生成新下联。
def couple_node(state: state):
    print(">>>>couple_node")
    writer = get_stream_writer()
    writer({"node": "couple_node"})

    latest_user_text = None
    for item in reversed(state.get("messages", [])):
        if isinstance(item, HumanMessage):
            latest_user_text = item.content
            break
        if isinstance(item, str):
            latest_user_text = item
            break
        if hasattr(item, "content") and isinstance(item.content, str):
            latest_user_text = item.content
            break

    raw_query = latest_user_text or memory_context(state)
    clean_query = normalize_couplet_query(raw_query)
    query = clean_query or raw_query
    prompt_template = ChatPromptTemplate.from_messages([
        (
            "system",
            "你是一个专业的对联大师。请根据用户最近一次提供的上联生成下联。\n"
            "若参考样例存在，必须优先按样例的语义结构、词性和节奏来写，不能自由发散创作；\n"
            "若参考样例为空，才可说明知识库中没有相关对联。\n"
            "请勿把历史反馈、上一轮回答或无关语句当成上联。\n"
            "参考对联：{samples}\n"
            "输出要求：只返回下联正文，不要带‘下联：’前缀，不要解释，不要说‘知识库中没有相关对联’除非样例为空，且回答不超过30字。",
        ),
        ("user", "上联：{text}")
    ])

    # 初始化 Embedding 模型，用于把查询和对联样例转成向量。
    embedding_model = OpenAIEmbeddings(
        model="text-embedding-v1",
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        tiktoken_enabled=False,
        check_embedding_ctx_length=False,
    )
    redis_url = os.getenv("REDIS_URL")
    redis_client = redis.from_url(redis_url)
    print('redis连接测试:', redis_client.ping())
    config = RedisConfig(
        index_name="couplet",
        redis_url=redis_url,
    )
    redisstore = RedisVectorStore(
        embeddings=embedding_model,
        config=config,
    )
    scord_results = redisstore.similarity_search_with_score(query, k=5)
    print('scord_results', scord_results)
    samples = []
    for doc, score in scord_results:
        print(doc, score)
        if getattr(doc, "page_content", None):
            samples.append(doc.page_content)

    samples_text = "\n".join(samples) if samples else "无参考对联"
    prompt = prompt_template.invoke({"text": query, "samples": samples_text})
    agent = create_agent(
        model=local_setting.qwen,
        checkpointer=False,
    )
    print(">>>>couplet prompts", prompt.messages)
    result = agent.invoke({"messages": prompt.messages})
    generated = result["messages"][-1].content.strip()
    for prefix in ("下联：", "上联："):
        if generated.startswith(prefix):
            generated = generated[len(prefix):].strip()
    generated = generated.replace("\n", "").strip()
    if samples and (generated in ("", "知识库中没有相关对联", "无参考对联")):
        generated = ""
    writer({">>>>couplet_result": generated})
    return {"messages": [AIMessage(generated or "对联生成失败")], "type": "couplet"}

# 兜底节点：当分类结果不明确或无有效能力时给出通用回复。
def other_node(state: state):
    print(">>>>other_node")
    writer = get_stream_writer()
    writer({"node": "other_node"})
    return {"messages": [AIMessage("无法回答您的问题")]}

# 笑话节点：根据用户需求生成一条简短笑话。
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

# 对 supervisor 的返回值进行分支选择，决定调用哪个节点继续处理。
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
# 创建 RedisSaver
checkpointer = AsyncRedisSaver(redis_url=redis_url)
#编译图
graph = builder.compile(checkpointer=checkpointer)

