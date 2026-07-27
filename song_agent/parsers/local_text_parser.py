from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any


def parse_local_text(path: Path, media_type: str, *, max_chars: int) -> tuple[str, dict[str, Any]]:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("文本文件包含二进制内容")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("文本文件不是 UTF-8 编码") from exc
    metadata: dict[str, Any] = {"encoding": "utf-8", "original_chars": len(text)}
    if media_type == "application/json":
        value = json.loads(text)
        metadata["json_type"] = type(value).__name__
        text = json.dumps(value, ensure_ascii=False, indent=2)
    elif media_type == "text/csv":
        rows = list(csv.reader(io.StringIO(text)))
        metadata["rows"] = len(rows)
        metadata["columns"] = max((len(row) for row in rows), default=0)
    metadata["truncated_on_read"] = len(text) > max_chars
    return text[:max_chars], metadata
