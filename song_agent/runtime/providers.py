from __future__ import annotations

from typing import Any

from ..kernel.errors import CatalogError
from ..kernel.module import ProviderContribution


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[type[Any], str], Any] = {}
        self._defaults: dict[type[Any], str] = {}

    def register(self, contribution: ProviderContribution) -> None:
        key = (contribution.port_type, contribution.provider_id)
        if key in self._providers:
            raise CatalogError(
                f"provider already registered: {contribution.port_type.__name__}/"
                f"{contribution.provider_id}"
            )
        self._providers[key] = contribution.provider
        if contribution.is_default:
            if contribution.port_type in self._defaults:
                raise CatalogError(
                    f"multiple default providers for {contribution.port_type.__name__}"
                )
            self._defaults[contribution.port_type] = contribution.provider_id

    def get(self, port_type: type[Any], provider_id: str | None = None) -> Any:
        selected = provider_id or self._defaults.get(port_type)
        if selected is None:
            raise CatalogError(f"no default provider configured for {port_type.__name__}")
        try:
            return self._providers[(port_type, selected)]
        except KeyError as error:
            raise CatalogError(
                f"provider not available: {port_type.__name__}/{selected}"
            ) from error

    def describe(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "port": port_type.__name__,
                "provider_id": provider_id,
                "default": self._defaults.get(port_type) == provider_id,
                "implementation": type(provider).__name__,
            }
            for (port_type, provider_id), provider in sorted(
                self._providers.items(), key=lambda item: (item[0][0].__name__, item[0][1])
            )
        )
