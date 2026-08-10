from __future__ import annotations

import httpx

from ..kernel.errors import DeliveryState, ProviderError
from ..ports.search import SearchHit


class TavilySearchProvider:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://api.tavily.com/search",
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.transport = transport

    async def search(
        self,
        query: str,
        *,
        provider: str = "auto",
        max_results: int = 5,
    ) -> tuple[SearchHit, ...]:
        if provider not in {"auto", "tavily"}:
            raise ProviderError(
                "search.provider_not_available",
                f"Search provider {provider!r} is not configured.",
                delivery_state=DeliveryState.NOT_SENT,
            )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport, trust_env=False
            ) as client:
                response = await client.post(
                    self.endpoint,
                    json={
                        "api_key": self.api_key,
                        "query": query,
                        "max_results": max_results,
                        "search_depth": "basic",
                    },
                )
        except httpx.HTTPError as error:
            raise ProviderError(
                "search.request_failed",
                "Search provider request failed.",
                retryable=True,
                delivery_state=DeliveryState.NOT_SENT,
            ) from error
        if response.is_error:
            raise ProviderError(
                "search.provider_rejected",
                f"Search provider returned HTTP {response.status_code}.",
                retryable=response.status_code == 429 or response.status_code >= 500,
                delivery_state=DeliveryState.REJECTED,
            )
        payload = response.json()
        return tuple(
            SearchHit(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or ""),
                source="tavily",
            )
            for item in payload.get("results", [])[:max_results]
            if isinstance(item, dict) and item.get("url")
        )
