"""上下文压缩的提示文案：L2 摘要请求的指令头，以及摘要替换历史后的消息前缀。"""

# 指令头之后空一行，再接整段历史消息的 JSON。
COMPACT_INSTRUCTION = (
    "为保持上下文连贯,请总结本次对话,内容需包含三点:\n"
    "1)已完成事项;2)当前状态;3)作出的关键决策。\n"
    "行文简洁,但务必保留核心关键信息。\n\n"
)

# 摘要替换整段历史后，作为唯一一条 user 消息的前缀。
COMPRESSED_PREFIX = "[Conversation compressed.]\n\n"

# L1 微压缩把旧工具结果替换成的占位符。
PREVIOUS_TOOL_PLACEHOLDER = "[Previous: used {tool_name}]"
