from pydantic import BaseModel, Field

from harness.domain.tool import (
    ALL_PROFILES,
    ALL_PROFILES_AND_DELEGATE,
    PydanticTool,
    ToolContext,
    tool_policy,
)


class WeatherInput(BaseModel):
    city: str = Field(description="City name")


class WeatherTool(PydanticTool):
    name = "Weather"
    description = "Search for weather conditions in any region."
    input_model = WeatherInput
    policy = tool_policy(
        "Weather",
        "leaf",
        bindable_profiles=ALL_PROFILES,
        callable_profiles=ALL_PROFILES_AND_DELEGATE,
    )

    async def run(self, args: WeatherInput, ctx: ToolContext) -> str:
        return f"{args.city} - 晴朗☀️"
