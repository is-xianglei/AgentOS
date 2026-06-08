from pydantic import BaseModel
from typing import Optional

class ToolUse(BaseModel):
    tid: str
    name: str
    input_schema: Optional[dict] = None

    def __str__(self):
        return str(self.__dict__)