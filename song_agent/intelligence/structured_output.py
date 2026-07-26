"""结构化输出公共约束。"""

from pydantic import BaseModel


class JsonObjectOutput(BaseModel):
    """标记模型输出来自 response_format=json_object。"""
