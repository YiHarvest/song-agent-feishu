"""飞书云文档的纯转换辅助（无 MCP 依赖）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class CreatedDocument:
    title: str
    token: str
    url: str


@dataclass
class FoundDocument:
    title: str
    token: str
    url: str


def sanitize_title(title: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]', " ", title)
    value = re.sub(r"\s+", " ", value).strip()
    return (value or "Agent 云文档")[:27]


def markdown_to_text_blocks(markdown: str, title: str) -> list[dict[str, Any]]:
    """将 Markdown 转换为安全的文本块，无需 Drive 导入权限范围。"""
    cleaned = markdown.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    for raw in re.split(r"\n\s*\n|\n", cleaned):
        value = raw.strip()
        if not value or value.startswith("```"):
            continue
        value = re.sub(r"^#{1,6}\s+", "", value)
        value = re.sub(r"^[-*+]\s+", "• ", value)
        value = re.sub(r"^\d+[.)]\s+", "", value)
        value = re.sub(r"(\*\*|__|`)", "", value).strip()
        if value == title and not paragraphs:
            continue
        paragraphs.extend(value[index : index + 1500] for index in range(0, len(value), 1500))
    if not paragraphs:
        paragraphs = ["待补充"]
    return [
        {
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": paragraph}}]},
        }
        for paragraph in paragraphs
    ]
