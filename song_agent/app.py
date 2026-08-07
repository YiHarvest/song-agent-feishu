"""FastAPI 应用模块（转发到 bootstrap）。

装配细节在 `bootstrap/app.py`；本模块只保留对外兼容入口。
"""

from __future__ import annotations

from collections.abc import Mapping

from .bootstrap.app import create_app

__all__ = ["create_app"]

app: FastAPI = create_app()


def _lark_sdk_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Restore canonical names expected by lark-oapi's case-sensitive lookup."""
    from .bootstrap.app import _lark_sdk_headers as _impl

    return _impl(headers)
