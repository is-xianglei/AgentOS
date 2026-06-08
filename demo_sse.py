"""
Demo 1: FastAPI + SSE 流式输出
展示 thinking / text / tool_use 三种 block 类型

运行: uv run uvicorn demo_sse:app --reload
访问: http://localhost:8000

原理：
  - GET /stream?prompt=... 返回 text/event-stream
  - 每个 Anthropic SDK 流事件序列化为 SSE data 帧
  - 前端用 EventSource 接收（浏览器原生支持 GET+SSE 自动重连）
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

HTML_FILE = Path(__file__).parent / "demo_sse.html"


@app.get("/")
async def index():
    return FileResponse(HTML_FILE)


@app.get("/stream")
async def stream_sse(prompt: str = "介绍量子纠缠，并查询北京天气"):
    """SSE 端点：每个 SDK 流事件转为一帧 SSE data"""
    async def generate():
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            thinking={"type": "adaptive", "display": "summarized"},
            tools=TOOLS,
            messages=[{"role": "user", "content": prompt}],
        ) as s:
            async for event in s:
                payload = event.model_dump(exclude_none=True)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
