from __future__ import annotations

import codecs
import hashlib
import os
import zipfile
from pathlib import Path


class UnsafeAttachmentError(ValueError):
    pass


def ensure_private_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved.chmod(0o700)
    return resolved


def ensure_within_root(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise UnsafeAttachmentError("附件存储路径无效")
    return resolved


def inspect_file(path: Path, *, declared_kind: str, filename: str) -> tuple[str, int, str]:
    size = path.stat().st_size
    if size <= 0:
        raise UnsafeAttachmentError("附件为空")
    with path.open("rb") as stream:
        head = stream.read(8192)
    media_type = _detect_media_type(head, path)
    if not _extension_matches(filename, media_type):
        raise UnsafeAttachmentError(f"附件扩展名与内容不匹配：{filename}")
    allowed = {
        "image": media_type.startswith("image/"),
        "audio": media_type.startswith("audio/"),
        "document": media_type
        in {
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/json",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/html",
        },
        "unknown": False,
    }
    if not allowed.get(declared_kind, False):
        raise UnsafeAttachmentError(f"附件内容与类型不匹配：{filename}")
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return media_type, size, sha.hexdigest()


def atomic_move(source: Path, destination: Path, root: Path) -> None:
    ensure_within_root(destination, root)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.replace(source, destination)
    destination.chmod(0o600)


def _detect_media_type(head: bytes, path: Path) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"#!AMR"):
        return "audio/amr"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "audio/mp4"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            raise UnsafeAttachmentError("Office 文件结构无效") from exc
        if any(name.startswith("word/") for name in names):
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if any(name.startswith("ppt/") for name in names):
            return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        if any(name.startswith("xl/") for name in names):
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        raise UnsafeAttachmentError("不支持的压缩文件")
    sample = head.lstrip()
    if b"\x00" in head:
        return "application/octet-stream"
    try:
        decoder = codecs.getincrementaldecoder("utf-8-sig")()
        text = decoder.decode(head, final=False)
    except UnicodeDecodeError:
        return "application/octet-stream"
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "application/json"
    if suffix == ".csv":
        return "text/csv"
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix in {".html", ".htm"} or sample.lower().startswith((b"<!doctype html", b"<html")):
        return "text/html"
    return "text/plain" if text else "application/octet-stream"


def _extension_matches(filename: str, media_type: str) -> bool:
    suffix = Path(filename).suffix.lower()
    if not suffix:
        return True
    expected = {
        ".txt": {"text/plain"},
        ".md": {"text/markdown"},
        ".markdown": {"text/markdown"},
        ".csv": {"text/csv"},
        ".json": {"application/json"},
        ".pdf": {"application/pdf"},
        ".docx": {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        },
        ".pptx": {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        },
        ".xlsx": {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        },
        ".html": {"text/html"},
        ".htm": {"text/html"},
        ".mp3": {"audio/mpeg"},
        ".wav": {"audio/wav"},
        ".ogg": {"audio/ogg"},
        ".m4a": {"audio/mp4"},
        ".amr": {"audio/amr"},
    }
    allowed = expected.get(suffix)
    return allowed is None or media_type in allowed
