"""审计元数据的保守式脱敏处理。"""

from __future__ import annotations

from typing import Any

_SECRET_MARKERS = (
    "token",
    "secret",
    "authorization",
    "oauth_code",
    "content",
    "markdown",
    "message",
    "body",
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "***"
                if any(marker in key.lower() for marker in _SECRET_MARKERS)
                else redact(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value[:50]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]
