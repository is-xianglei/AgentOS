from anthropic.types import ToolParam
from pydantic import BaseModel
from typing import Any, Type
from abc import ABC, abstractmethod

class BaseTool(ABC):
    name: str
    description: str
    input_model: Type[BaseModel]

    def to_param(self) -> ToolParam:
        return ToolParam(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
        )

    def run_with_dict(self, data: dict[str, Any]) -> str:
        input_object = self.input_model.model_validate(data)
        return self.run(input_object)

    @abstractmethod
    def run(self, input_object: BaseModel) -> str:
        pass