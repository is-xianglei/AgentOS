PLAN_MODE_INSTRUCTIONS = """## Plan Mode 已启用

用户当前只允许你探索和规划，不允许执行实现。此约束高于普通任务指令。

你必须遵守：
1. 仅使用只读工具探索代码、配置和现有架构。
2. 使用 AskUserQuestion 澄清只能由用户决定的需求或取舍；不要询问可从代码中查明的事实。
3. 持续使用 WritePlan 完整替换唯一的 Markdown 计划正文。除计划记录外不得修改任何内容。
4. 计划应包含背景、推荐方案、关键文件与可复用实现、边界条件和端到端验证方式。
5. 计划完成后调用 ExitPlanMode 请求批准。不要通过普通文本或 AskUserQuestion 请求计划批准。
6. 在用户拒绝退出后，根据反馈修改计划并再次调用 ExitPlanMode。

当前计划正文：
{plan_content}
"""

ENTER_PLAN_MODE_RESULT = """已进入Plan Mode。现在只探索代码并设计实现方案。

除使用 WritePlan 更新计划外，不得写入或编辑任何内容。需要澄清时使用
AskUserQuestion；计划完整后必须使用 ExitPlanMode 请求用户批准。"""


def render_plan_mode_instructions(content: str) -> str:
    current = content.strip() or "（尚未创建，请尽快使用 WritePlan 写入计划骨架。）"
    return PLAN_MODE_INSTRUCTIONS.format(plan_content=current)
