# MultiAgent

一个基于 LangGraph 的多智能体助手示例项目，使用通义千问完成问题分类和回答，并集成：

- 旅游路线规划：通过高德地图 MCP 服务获取路线信息
- 对联生成：使用 Redis 向量数据库检索参考对联
- 笑话生成
- 其它问题的兜底回复

## 项目结构

```text
.
├── api.py                 # FastAPI 服务入口
├── Director.py            # 多智能体工作流和节点定义
├── DirectorServer.py      # 本地调用测试脚本
├── local_setting.py       # 通义千问模型配置
├── coupletLoader.py       # 将 resource/test.csv 导入 Redis 向量库
├── CoupletRetraval.py     # 对联检索测试脚本
├── docker-compose.yml     # Redis Stack 服务配置
├── requirements.txt       # Python 依赖
└── resource/test.csv      # 对联数据
```

## 环境要求

- Python 3.12 或兼容版本
- Docker Desktop
- Node.js 和 npm
- 可用的通义千问 API Key
- 可用的高德地图 API Key（使用旅游路线功能时需要）

## 安装依赖

创建并激活虚拟环境：

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

安装 Python 依赖：

```powershell
pip install -r requirements.txt
```

高德地图 MCP 服务由 `npx` 在运行时调用，因此需要确保 Node.js/npm 已加入 PATH。

## 配置环境变量

在项目根目录创建 `.env` 文件。不要把真实 `.env` 文件上传到 Git，也不要将真实 Key 写入 README、代码或示例文件。

```dotenv
DASHSCOPE_API_KEY=your_dashscope_api_key
AMAP_MAPS_API_KEY=your_amap_maps_api_key
REDIS_URL=redis://:your_redis_password@localhost:6379
```

项目使用 `python-dotenv` 加载 `.env`。相关变量用途如下：

| 变量 | 用途 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 通义千问模型和文本向量服务 |
| `AMAP_MAPS_API_KEY` | 高德地图 MCP 服务 |
| `REDIS_URL` | Redis 连接地址 |

## 启动 Redis

```powershell
docker compose up -d redis
```

检查容器状态：

```powershell
docker compose ps
```

默认 Redis 端口为 `6379`。`REDIS_URL` 中的密码需要与 `docker-compose.yml` 中本地 Redis 的密码保持一致。

## 导入对联向量数据

确保 Redis 已启动并且 `.env` 配置正确，然后运行：

```powershell
python coupletLoader.py
```

该脚本会读取 `resource/test.csv`，生成文本向量并写入 Redis 的 `couplet` 索引。

## 运行测试脚本

测试对联检索和生成：

```powershell
python CoupletRetraval.py
```

测试完整工作流：

```powershell
python DirectorServer.py
```

## 启动 API 服务

```powershell
python api.py
```

服务默认监听 `http://127.0.0.1:8001`。

调用示例：

```powershell
curl.exe -X POST "http://127.0.0.1:8001/api/director" `
  -H "Content-Type: application/json" `
  -d '{"query":"请帮我讲一个笑话"}'
```

接口返回示例：

```json
{
  "query": "请帮我讲一个笑话",
  "response": "..."
}
```

## 安全说明

- `.env` 已加入 `.gitignore`，不要使用 `git add -f .env` 强制提交。
- 如果 API Key 曾经出现在聊天记录、日志、截图或公开仓库中，应及时在对应平台轮换。
- Redis 密码当前用于本地测试；生产环境请使用独立密码，并通过环境变量或密钥管理服务注入。
