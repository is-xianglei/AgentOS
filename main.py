from anthropic import Anthropic
from anthropic.types import MessageParam, ToolParam
from anthropic.types.tool_param import InputSchemaTyped
from dotenv import load_dotenv
from pathlib import Path
import os

root_dir = Path(__file__).resolve().parent

load_dotenv(root_dir / ".env")

client = Anthropic(
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
    api_key=os.getenv("ANTHROPIC_API_KEY"),
)

def _build_tool(name: str, description: str, required: list[str], properties: dict[str, object]) -> ToolParam:
    return ToolParam(
        name=name,
        description=description,
        input_schema=InputSchemaTyped(
            type="object",
            required=required,
            properties=properties
        )
    )


def bash(command: str) -> str:
    return f"bash -c '{command}'"

tools = [
    _build_tool(name="bash", description="Run a shell command.", required=["command"], properties={"command": {"type": "string"}}),
    _build_tool(name="weather", description="Get weather forecast.", required=["command"], properties={"command": {"type": "string"}}),
]

tool_handlers = {
    "bash": bash
}


def loop():
    messages = client.messages.create(
        max_tokens=1024,
        model=os.getenv("ANTHROPIC_MODEL"),
        stream=True,
        system="你是一个乐于助人的助手.",
        messages=[
            MessageParam(content="你好!先介绍一下自己，然后帮我在当前工作目录创建一个test.txt文件,然后在使用工具帮我查询天气预报", role="user")
        ],
        tools=tools
    )

    for event in messages:
        if event.type == "content_block_start":
            block = event.content_block
            if block.type == "thinking":
                print("thinking:", end="", flush=True)
            if block.type == "text":
                print("\nAi:", end="", flush=True)
            if block.type == "tool_use":
                tool_id = block.id
                tool_name = block.name
                print("\nAi_Tool:", end="", flush=True)
        if event.type == "content_block_delta":
            delta = event.delta
            if delta.type == "thinking_delta":
                print(delta.thinking, end="", flush=True)
            if delta.type == "text_delta":
                print(delta.text, end="", flush=True)
            if delta.type == "input_json_delta":
                print(delta.partial_json, end="", flush=True)
        if event.type == "content_block_stop":
            print()

if __name__ == '__main__':
    loop()
