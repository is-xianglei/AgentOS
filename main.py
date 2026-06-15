from anthropic.types import MessageParam, ToolResultBlockParam
from llm_client import client, LLM_MODEL
from tools import tools, tool_handlers

event_type: list[str] = ["message_start", "message_delta", "message_stop", "content_block_start", "content_block_delta", "content_block_stop"]

def loop():

    messages_history: list[MessageParam] = [
        MessageParam(role="user",content="派发两个子代理帮我翻译：你好世界！\n 一个翻译为英文，一个翻译为日文。")
    ]

    while True:
        with client.messages.stream(
                max_tokens=1024,
                model=LLM_MODEL,
                system="你是一个乐于助人的助手.",
                messages=messages_history,
                tools=tools
        ) as stream:
            for block in stream:
                if block.type in event_type:
                    yield f"data: {block.model_dump_json(warnings=False)}\n\n"

            final_message = stream.get_final_message()

            # 回传LLM消息
            messages_history.append(MessageParam(role="assistant", content=final_message.content))

            # 判断是否调用工具
            if final_message.stop_reason != 'tool_use':
                break

            # 解析并调用工具
            tool_results: list[ToolResultBlockParam] = []
            for item in final_message.content:
                if item.type != 'tool_use':
                    continue
                tool_name: str = item.name
                tool_input: dict = item.input
                handler = tool_handlers[tool_name]
                tool_result = ToolResultBlockParam(
                    type='tool_result',
                    tool_use_id=item.id,
                    content=handler.run_with_dict(tool_input)
                )
                tool_results.append(tool_result)

            # 回传工具调用结果
            messages_history.append(MessageParam(role="user", content=tool_results))



if __name__ == '__main__':
    gen = loop()
    for event in gen:
        print(event)













# app = FastAPI()
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_methods=["GET"],
#     allow_headers=["*"],
# )
#
# @app.get("/stream")
# def agent(prompt: str):
#     return StreamingResponse(
#         loop(),
#         media_type="text/event-stream; charset=utf-8",
#         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
#     )
