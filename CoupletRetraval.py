#测试对联数据
import sys
from pathlib import Path
import local_setting
import redis

from dotenv import load_dotenv
import os
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_redis import RedisVectorStore,RedisConfig
from langchain_core.prompts import ChatPromptTemplate
from langchain.agents import create_agent
from langchain_openai import OpenAIEmbeddings
# 加载 .env 文件中的环境变量
load_dotenv()
# 初始化Embeddingse
embedding_model = OpenAIEmbeddings(
    model="text-embedding-v1",
    api_key= os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    tiktoken_enabled=False,  # 禁用 tiktoken 编码器，避免兼容性问题
    check_embedding_ctx_length=False,  # 禁用上下文长度检查
)
redis_url = os.getenv("REDIS_URL")
redis_client = redis.from_url(redis_url)
print(redis_client.ping()) #测试连接，返回true,表示连接成功
config = RedisConfig(
    index_name="couplet",
    redis_url=redis_url,
)
redisstore= RedisVectorStore(
    embeddings=embedding_model,
    config=config,
)
query="上联是：瑞雪兆丰年"
scord_results = redisstore.similarity_search_with_score(query, k=5)
samples = []
for doc, score in scord_results: 
    print(doc, score)
    samples.append(doc.page_content)
prompt_template = ChatPromptTemplate.from_messages([
    ("system", "你是一个专业的对联大师，你的任务是根据用户给出的上联，设计一下下联\n    回答时可以参考下面的参考对联。\n     参考对联：{samples}\n请用中文回答"),
    ("user", "上联是：{text}")
])
prompt = prompt_template.invoke({"text": query, "samples": samples})  # ✅ 正确
print(111,prompt)
result = local_setting.qwen.invoke(prompt)
print('-'*100)
print(result.content)