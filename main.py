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

def bash(command: str) -> str:
    return f"bash -c '{command}'"

tools = [
    ToolParam(
        name="bash",
        description="Run a shell command.",
        input_schema=InputSchemaTyped(
            type="object",
            required=["command"],
            properties={
                "command": {
                    "type": "string"
                }
            }
        )
    )
]

tool_handlers = {
    "bash": bash
}


def loop():
    messages = client.messages.create(
        max_tokens=1024,
        model=os.getenv("ANTHROPIC_MODEL"),
        stream=True,
        system="你是一个不废话的并且乐于助人的助手.",
        messages=[
            MessageParam(content="你好!帮我在当前工作目录创建一个test.txt文件", role="user")
        ],
        tools=tools
    )

    for event in messages:
        print(event)
        # if event.type == "content_block_start" and event.content_block.type == "thinking":
        #     print("深度思考:", end="", flush=True)
        # if event.type == "content_block_start" and event.content_block.type == "text":
        #     print("Ai:", end="", flush=True)
        # if event.type == "content_block_start" and event.content_block.type == "tool_use":
        #     print(event)
        # if event.type == "content_block_stop":
        #     print()
        # if event.type == "content_block_delta" and event.delta.type == "thinking_delta":
        #     print(event.delta.thinking, end="", flush=True)
        # if event.type == "content_block_delta" and event.delta.type == "text_delta":
        #     print(event.delta.text, end="", flush=True)


if __name__ == '__main__':
    loop()
