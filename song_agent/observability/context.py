"""HTTP、WebSocket 和作业的请求无关追踪上下文。"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def current_trace_id() -> str:
    return _trace_id.get() or uuid.uuid4().hex


@contextmanager
def trace_scope(trace_id: str = "") -> Iterator[str]:
    value = trace_id or uuid.uuid4().hex
    token = _trace_id.set(value)
    try:
        yield value
    finally:
        _trace_id.reset(token)
