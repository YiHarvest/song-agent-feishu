"""Validated identifiers shared by the kernel."""

from __future__ import annotations

import re

_INTENT_PATTERN = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
_MODULE_PATTERN = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")
RESERVED_INTENT_NAMESPACES = frozenset({"system", "action", "internal", "agent"})


def validate_intent_id(value: str) -> str:
    normalized = value.strip()
    if not _INTENT_PATTERN.fullmatch(normalized):
        raise ValueError(
            "intent id must contain at least two lowercase dot-separated segments"
        )
    return normalized


def validate_module_id(value: str) -> str:
    normalized = value.strip()
    if not _MODULE_PATTERN.fullmatch(normalized):
        raise ValueError("module id must be lowercase kebab-case")
    return normalized


def top_level_namespace(intent_id: str) -> str:
    return validate_intent_id(intent_id).partition(".")[0]
