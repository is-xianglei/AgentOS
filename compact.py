import json

from anthropic.types import MessageParam

from llm_client import client, LLM_MODEL


# L1 - 微压缩
def micro_compact(messages: list[MessageParam]) -> list[MessageParam]:
    tool_results = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get('role', None) != 'user':
            continue
        if not isinstance(message.get('content', None), list):
            continue
        for content in message.get('content', []):
            if content.get('type', None) != 'tool_result':
                continue
            tool_results.append(content)

    # 没有超过 3 轮及以上的旧工具调用原样返回
    if len(tool_results) <= 3:
        return messages

    tool_name_map: dict[str, str] = {}
    for message in messages:
        if message.get('role', None) != 'assistant':
            continue
        if not isinstance(message.get('content', None), list):
            continue
        for content_item in message.get('content', []):
            if content_item.type == 'tool_use':
                # 工具ID : 工具名称
                tool_name_map[content_item.id] = content_item.name

    # 保留最近 3 条工具结果不压缩
    for result_item in tool_results[:3]:
        # 非字符串跳过,因为有些工具的结果可能是多模态，无法安全替换为字符串。
        if not isinstance(result_item['content'], str):
            continue
        # 工具结果字符串少于 100字符的跳过，压缩收益太小，不值得
        if len(result_item['content']) <= 100:
            continue

        # 保留 read_file 的输出，因为它们是参考资料；压缩会迫使智能体重新读取文件
        tool_id: str = result_item['tool_use_id']
        tool_name = tool_name_map[tool_id]
        if tool_name in ['read_file']:
            continue
        result_item['content'] = f"[Previous: used {tool_name}]"

    return messages


# L2 - 自动压缩
def auto_compact(messages: list) -> list:
    # TODO 压缩前要把原始消息存储起来.

    compact_prompt: str = f'''
        为保持上下文连贯，请总结本次对话，内容需包含三点：
        1）已完成事项；2）当前状态；3）作出的关键决策。
        行文简洁，但务必保留核心关键信息。\n\n
        {json.dumps(messages, default=str)}            
    '''

    response = client.messages.create(
        model=LLM_MODEL,
        messages=[MessageParam(role="user", content=compact_prompt)],
        max_tokens=1024
    )

    summary = next((block.text for block in response.content if hasattr(block, "text")), "")

    # TODO 这里也要拼接上以前会话的地址信息引用。可以让Ai找到来源。
    return [MessageParam(role="user", content=f'[Conversation compressed.]\n\n{summary}')]


# 粗略 token 计数：约 4 个字符对应 1 个 token。
def estimate_tokens(messages: list) -> int:
    return len(str(messages)) // 4
