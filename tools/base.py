from anthropic.types import ToolParam
from pydantic import BaseModel, ValidationError
from typing import Any, Type
from abc import ABC, abstractmethod
import json

class BaseTool(ABC):
    name: str
    description: str
    input_model: Type[BaseModel]

    def to_param(self) -> ToolParam:
        return ToolParam(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema() if self.input_model else {},
        )

    def run_with_dict(self, data: dict[str, Any]) -> str:
        if data:
            try:
                input_object = self.input_model.model_validate(data)
            except ValidationError as err:
                return f'error: {str(err)}. hint:Please fix the parameters and try again'
            return self.run(input_object)
        else:
            return json.dumps({})

    @abstractmethod
    def run(self, input_object: BaseModel) -> str:
        pass