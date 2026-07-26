"""统一结果渲染。"""

from __future__ import annotations

from ..domain.results import ApplicationResult


def render_message(result: ApplicationResult) -> str:
    if result.status == "authorization_required" and result.authorization_url:
        return f"{result.message}[点击这里完成授权]({result.authorization_url})"
    return result.message
