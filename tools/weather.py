from pydantic import BaseModel, Field

from tools.base import BaseTool

class WeatherInput(BaseModel):
    city: str = Field(description='City name')

class WeatherTool(BaseTool):
    name = 'Weather'
    description = 'Search for weather conditions in any region.'
    input_model = WeatherInput

    def run(self, input_object: WeatherInput) -> str:
        return f'{input_object.city} - 晴朗☀️'