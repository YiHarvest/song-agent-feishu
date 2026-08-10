"""Unified module contribution protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .intent import IntentSpec


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    module_id: str
    version: str
    owns_namespaces: frozenset[str]
    requires: tuple[str, ...] = ()
    optional_requires: tuple[str, ...] = ()
    schema_version: str = "1"


@dataclass(frozen=True, slots=True)
class ProviderContribution:
    port_type: type[Any]
    provider_id: str
    provider: Any
    is_default: bool = False


class ContributionSink(Protocol):
    def add_intent(self, spec: IntentSpec) -> None: ...

    def add_provider(self, contribution: ProviderContribution) -> None: ...

    def add_lifecycle(self, resource: Any) -> None: ...

    def add_context_provider(self, provider_id: str, provider: Any) -> None: ...


class Module(Protocol):
    manifest: ModuleManifest

    def contribute(self, sink: ContributionSink) -> None: ...
