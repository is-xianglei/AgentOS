"""
Demo 2: FastAPI + Streamable HTTP 流式输出
展示 thinking / text / tool_use 三种 block 类型

运行: uv run uvicorn demo_streamable_http:app --reload --port 8001
访问: http://localhost:8001

原理：
  - POST /stream  body: {"prompt": "..."}
  - 返回 application/x-ndjson（换行分隔 JSON）
  - 前端用 fetch + ReadableStream 手动读取，不依赖 EventSource
  - 与 SSE 的核心区别：
      SSE   → GET + text/event-stream，浏览器 EventSource 自动重连，只能单向服务端→客户端
      HTTP  → POST + 任意 body，手动解析，支持请求携带 JSON body
"""
import json
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
import anthropic

app = FastAPI()
client = anthropic.AsyncAnthropic()

TOOLS = [{
    "name": "get_weather",
    "description": "获取指定城市的实时天气。当用户询问天气时调用此工具。",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名称，如 Beijing"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"], "description": "温度单位"}
        },
        "required": ["city"]
    }
}]

HTML_FILE = Path(__file__).parent / "demo_streamable_http.html"


@app.get("/")
async def index():
    return FileResponse(HTML_FILE)


@app.post("/stream")
async def stream_http(body: dict):
    """Streamable HTTP 端点：每个 SDK 流事件序列化为一行 JSON（ndjson）"""
    prompt = body.get("prompt", "介绍量子纠缠，并查询北京天气")

    async def generate():
        async with client.messages.stream(
            model="claude-opus-4-8",
            max_tokens=4096,
            thinking={"type": "adaptive", "display": "summarized"},
            tools=TOOLS,
            messages=[{"role": "user", "content": prompt}],
        ) as s:
            async for event in s:
                payload = event.model_dump(exclude_none=True)
                # ndjson: 每行一个完整 JSON 对象，以换行符分隔
                yield json.dumps(payload, ensure_ascii=False) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )
