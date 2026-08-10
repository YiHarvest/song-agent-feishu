from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    source: str


class SearchPort(Protocol):
    async def search(
        self,
        query: str,
        *,
        provider: str = "auto",
        max_results: int = 5,
    ) -> tuple[SearchHit, ...]: ...
