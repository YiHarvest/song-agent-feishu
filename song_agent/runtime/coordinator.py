from __future__ import annotations

import asyncio
from collections import defaultdict

from ..kernel.models import RequestEnvelope


class ExecutionCoordinator:
    """Serialize natural-language and agent work per conversation, not explicit intents."""

    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock_for(self, envelope: RequestEnvelope) -> asyncio.Lock:
        key = envelope.conversation.key if envelope.conversation else envelope.request_id
        return self._locks[key]
