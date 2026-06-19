from anthropic.types import MessageParam, ToolResultBlockParam
from tools.subagents.definition import AgentDefinition
from llm_client import client, LLM_MODEL
from tools import tool_handlers

general_purpose_system_prompt = f"""
When you complete the task, respond with a concise report covering what was done and any key findings — the caller will relay this to the user, so it only needs the essentials.
"""

class GeneralPurposeAgent(AgentDefinition):

    def run(self, prompt: str) -> str: # noqa: no-self-use

        sub_messages: list[MessageParam] = [
            MessageParam(role="user", content=prompt)
        ]

        while True:
            with client.messages.stream(
                    max_tokens=1024,
                    model=LLM_MODEL,
                    system=general_purpose_system_prompt,
                    messages=sub_messages,
                    tools=self.resolve_agent_tools()
            ) as stream:

                final_message = stream.get_final_message()

                # 回传LLM消息
                sub_messages.append(MessageParam(role="assistant", content=final_message.content))

                # 判断是否调用工具
                if final_message.stop_reason != 'tool_use':
                    # print(f'sub_agent:{'\n'.join(block.text for block in final_message.content if block.type == 'text')}')
                    return '\n'.join(block.text for block in final_message.content if block.type == 'text')
                else:
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
                    sub_messages.append(MessageParam(role="user", content=tool_results))