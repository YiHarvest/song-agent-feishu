"""OpenAI-compatible request and response schemas for the external Agent API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = Field(description="消息角色。支持 system、developer、user、assistant；不支持 tool。")
    content: Any = Field(description="消息正文。当前只支持纯文本字符串。")
    name: str | None = Field(default=None, description="可选消息发送者名称。")


class StreamOptions(BaseModel):
    include_usage: bool = Field(
        default=False,
        description="流式响应结束前是否额外返回 token 用量块。",
    )


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "example": {
                "model": "song-agent-2.1",
                "messages": [
                    {
                        "role": "user",
                        "content": "你好，请介绍一下自己。",
                    }
                ],
                "stream": False,
                "user": "mentor-user",
                "metadata": {"conversation_id": "mentor-thread-1"},
            }
        },
    )

    model: str = Field(
        min_length=1,
        description="模型标识，必须等于 SONG_AGENT_API_MODEL_ID。",
    )
    messages: list[ChatMessage] = Field(
        min_length=1,
        description="OpenAI Chat Completions 消息数组。",
    )
    stream: bool = Field(
        default=False,
        description="是否使用 Server-Sent Events 流式返回。",
    )
    stream_options: StreamOptions | None = Field(
        default=None,
        description="流式响应附加选项；stream=true 时使用。",
    )
    temperature: float | None = Field(
        default=None,
        description="生成温度，兼容接收并传入 Agent 请求上下文。",
    )
    top_p: float | None = Field(
        default=None,
        description="核采样参数，兼容接收并传入 Agent 请求上下文。",
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description="兼容字段：最大输出 token 数。",
    )
    max_completion_tokens: int | None = Field(
        default=None,
        ge=1,
        description="兼容字段：最大 completion token 数，优先于 max_tokens。",
    )
    stop: str | list[str] | None = Field(
        default=None,
        description="遇到指定文本时截断最终输出。",
    )
    n: int = Field(
        default=1,
        ge=1,
        description="生成结果数量。当前只支持 1。",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=("扩展元数据。可传 conversation_id、delivery_channel、delivery_binding_id。"),
    )
    user: str | None = Field(
        default=None,
        description="API 用户标识；X-Song-User-Id 请求头优先。",
    )
    tools: Any = Field(
        default=None,
        description="暂不支持；传入此字段会返回 400。",
    )
    tool_choice: Any = Field(
        default=None,
        description="暂不支持；传入此字段会返回 400。",
    )


class BindingCodeResponse(BaseModel):
    code: str = Field(description="一次性飞书绑定码。")
    command: str = Field(description="需要在飞书中发送的完整绑定命令。")
    expires_at: int = Field(description="绑定码过期时间，Unix 秒时间戳。")
