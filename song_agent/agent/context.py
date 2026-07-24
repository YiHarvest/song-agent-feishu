"""Agent 单次运行的身份绑定上下文。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ..models import IncomingMessage


@dataclass(slots=True)
class AgentContext:
    message: IncomingMessage
    user_text: str
    conversation_key: str
    metadata: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
