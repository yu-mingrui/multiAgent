#把对联数据保存到redis 向量数据库中
import sys
from pathlib import Path

import local_setting
import redis

from dotenv import load_dotenv
import os
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_redis import RedisVectorStore, RedisConfig
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

#保存向量数据库
redis_url = os.getenv("REDIS_URL")
redis_client = redis.from_url(redis_url)
print(redis_client.ping())  # 测试连接，返回true,表示连接成功
config = RedisConfig(
    index_name="couplet",
    redis_url=redis_url,
)

lines = []
resource_path ="resource/test.csv"
with open(resource_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            # print(line)
            lines.append(line)

# DashScope/OpenAI 兼容 embedding API 的单次 batch 上限是 25，
# 因此需要按批次写入，不要一次性把全部 CSV 文本一起送入模型。
BATCH_SIZE = 25
for index in range(0, len(lines), BATCH_SIZE):
    batch = lines[index:index + BATCH_SIZE]
    RedisVectorStore.from_texts(
        texts=batch,
        embedding=embedding_model,
        config=config,
    )