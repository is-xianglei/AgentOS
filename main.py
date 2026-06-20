from anthropic.types import MessageParam, ToolResultBlockParam
from llm_client import client, LLM_MODEL
from tools import tools, tool_handlers
from compact import auto_compact, micro_compact, estimate_tokens

event_type: list[str] = ["message_start", "message_delta", "message_stop", "content_block_start", "content_block_delta", "content_block_stop"]

def loop():

    messages_history: list[MessageParam] = [
        MessageParam(role="user",content="创建一个Team，只要有 3 个成员参与任务。帮我创建一个txt文件，并查询上海的天气写入进去.")
    ]

    while True:
        # L1: 每次 LLM 调用前执行 micro_compact
        micro_compact(messages_history)

        # L2: 若 token 估算超过阈值则触发 auto_compact
        if estimate_tokens(messages_history) > 50000:
            print('-debug auto_compact 已触发')
            messages_history[:] = auto_compact(messages_history)

        with client.messages.stream(
                max_tokens=1024,
                model=LLM_MODEL,
                system="Spawn teammates and communicate via mailbox.",
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
                if tool_name == 'compact':
                    messages_history[:] = auto_compact(messages_history)
                    tool_result = ToolResultBlockParam(
                        type='tool_result',
                        tool_use_id=item.id,
                        content='Compressing...'
                    )
                    tool_results.append(tool_result)
                else:
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
