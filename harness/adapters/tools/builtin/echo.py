from pydantic import BaseModel, Field

from harness.domain.tool import (
    ALL_PROFILES,
    ALL_PROFILES_AND_DELEGATE,
    PydanticTool,
    ToolContext,
    tool_policy,
)


class EchoInput(BaseModel):
    text: str = Field(description="要回显的文本")


class EchoTool(PydanticTool):
    name = "echo"
    description = "回显输入文本，用于验证工具链路。"
    input_model = EchoInput
    # 运行时名称区分大小写，策略名必须与小写的 ``echo`` 完全一致，否则目录同步后的
    # 用户 Agent 绑定会在解析阶段被误判为未知工具。
    policy = tool_policy(
        "echo",
        "leaf",
        bindable_profiles=ALL_PROFILES,
        callable_profiles=ALL_PROFILES_AND_DELEGATE,
    )

    async def run(self, args: EchoInput, ctx: ToolContext) -> str:
        return args.text
