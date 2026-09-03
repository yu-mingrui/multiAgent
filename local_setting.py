
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
# 加载 .env 文件中的环境变量
load_dotenv()

# 从环境变量获取 API Key
api_key = os.getenv("DASHSCOPE_API_KEY")

qwen = ChatOpenAI(api_key=api_key,
                  model="qwen3.8-max",
                  base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                  
)