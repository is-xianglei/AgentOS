"""
FastAPI Streamable HTTP 流式后端 — 基于 main.py 逻辑

运行: uv run uvicorn main_streamable:app --reload --port 8001
前端: 用浏览器直接打开 main_streamable.html（任意目录均可）
"""
import json
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolParam
from anthropic.types.tool_param import InputSchemaTyped
from model import ToolUse
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

client = AsyncAnthropic(
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_API_KEY"),
)


def _build_tool(name: str, description: str, required: list[str], properties: dict) -> ToolParam:
    return ToolParam(
        name=name,
        description=description,
        input_schema=InputSchemaTyped(type="object", required=required, properties=properties),
    )


tools = [
    _build_tool("bash", "Run a shell command.", ["command"], {"command": {"type": "string"}}),
    _build_tool("weather", "Get weather forecast.", ["city"], {"city": {"type": "string"}}),
]


def bash(command: str) -> str:
    return f"[模拟执行] bash -c '{command}'"


def weather(city: str) -> str:
    return f"[模拟天气] {city}: 晴，25°C，东南风 3 级"


tool_handlers = {
    "bash": bash,
    "weather": weather,
}


@app.post("/stream")
async def stream_http(body: dict):
    prompt = body.get("prompt", "你好！先介绍一下自己，然后帮我在当前目录创建 test.txt，再查询北京天气预报")

    async def generate():
        messages: list[MessageParam] = [MessageParam(role="user", content=prompt)]

        while True:
            tool_uses: list[ToolUse] = []
            current_tool: ToolUse | None = None
            input_buf = ""

            async with client.messages.stream(
                model=os.getenv("ANTHROPIC_MODEL"),
                max_tokens=1024,
                system="你是一个乐于助人的助手.",
                messages=messages,
                tools=tools,
            ) as s:
                async for event in s:
                    yield json.dumps(event.model_dump(exclude_none=True), ensure_ascii=False) + "\n"

                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            current_tool = ToolUse(tid=block.id, name=block.name)
                            input_buf = ""
                    elif event.type == "content_block_delta":
                        if event.delta.type == "input_json_delta":
                            input_buf += event.delta.partial_json
                    elif event.type == "content_block_stop":
                        if current_tool is not None:
                            try:
                                current_tool.input_schema = json.loads(input_buf) if input_buf else {}
                            except json.JSONDecodeError:
                                current_tool.input_schema = {}
                            tool_uses.append(current_tool)
                            current_tool = None
                            input_buf = ""

                final = await s.get_final_message()

            messages.append(MessageParam(role="assistant", content=final.content))

            if final.stop_reason != "tool_use" or not tool_uses:
                break

            tool_results = []
            for tu in tool_uses:
                handler = tool_handlers.get(tu.name)
                result = handler(**tu.input_schema) if handler else f"未知工具: {tu.name}"

                custom = {"type": "tool_executing", "name": tu.name, "input": tu.input_schema, "result": result}
                yield json.dumps(custom, ensure_ascii=False) + "\n"

                tool_results.append({"type": "tool_result", "tool_use_id": tu.tid, "content": result})

            messages.append(MessageParam(role="user", content=tool_results))

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )
