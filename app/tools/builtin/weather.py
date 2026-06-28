from pydantic import BaseModel, Field

from app.tools.base import BaseTool, ToolContext


class WeatherInput(BaseModel):
    city: str = Field(description="City name")


class WeatherTool(BaseTool):
    name = "Weather"
    description = "Search for weather conditions in any region."
    input_model = WeatherInput

    async def run(self, args: WeatherInput, ctx: ToolContext) -> str:
        return f"{args.city} - 晴朗☀️"
