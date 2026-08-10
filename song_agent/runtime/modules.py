from __future__ import annotations

from collections import defaultdict
from importlib import metadata
from typing import Any

from ..kernel.errors import CatalogError
from ..kernel.intent import IntentSpec
from ..kernel.module import Module, ProviderContribution
from .catalog import IntentCatalog
from .context import ContextAssembler
from .providers import ProviderRegistry


class ContributionRegistry:
    def __init__(
        self,
        *,
        catalog: IntentCatalog,
        providers: ProviderRegistry,
        contexts: ContextAssembler,
    ) -> None:
        self.catalog = catalog
        self.providers = providers
        self.contexts = contexts
        self.lifecycle: list[Any] = []

    def add_intent(self, spec: IntentSpec) -> None:
        self.catalog.register(spec)

    def add_provider(self, contribution: ProviderContribution) -> None:
        self.providers.register(contribution)

    def add_lifecycle(self, resource: Any) -> None:
        self.lifecycle.append(resource)

    def add_context_provider(self, provider_id: str, provider: Any) -> None:
        self.contexts.register(provider_id, provider)


class ModuleRuntime:
    def __init__(
        self,
        modules: tuple[Module, ...],
        *,
        catalog: IntentCatalog,
        providers: ProviderRegistry,
        contexts: ContextAssembler,
    ) -> None:
        self.modules = _topological_modules(modules)
        self.catalog = catalog
        self.providers = providers
        self.contexts = contexts
        self.registry = ContributionRegistry(
            catalog=catalog,
            providers=providers,
            contexts=contexts,
        )
        self.module_versions: dict[str, str] = {}

    def assemble(self) -> ContributionRegistry:
        for module in self.modules:
            manifest = module.manifest
            self.catalog.declare_module(manifest.module_id, manifest.owns_namespaces)
            self.module_versions[manifest.module_id] = manifest.version
        for module in self.modules:
            module.contribute(self.registry)
        return self.registry

    async def start(self) -> None:
        for resource in self.registry.lifecycle:
            start = getattr(resource, "start", None)
            if start is not None:
                await start()

    async def close(self) -> None:
        for resource in reversed(self.registry.lifecycle):
            close = getattr(resource, "close", None)
            if close is not None:
                await close()


def discover_external_modules(*, enabled: bool, allowlist: frozenset[str]) -> tuple[Module, ...]:
    if not enabled:
        return ()
    discovered: list[Module] = []
    for entry_point in metadata.entry_points(group="song_agent.modules"):
        if entry_point.name not in allowlist:
            continue
        module = entry_point.load()()
        if getattr(module.manifest, "schema_version", "1") != "1":
            raise CatalogError(
                f"external module {entry_point.name!r} cannot contribute schema migrations"
            )
        discovered.append(module)
    missing = allowlist - {entry_point.name for entry_point in metadata.entry_points(
        group="song_agent.modules"
    )}
    if missing:
        raise CatalogError(f"allowed external modules not installed: {', '.join(sorted(missing))}")
    return tuple(discovered)


def _topological_modules(modules: tuple[Module, ...]) -> tuple[Module, ...]:
    by_id: dict[str, Module] = {}
    for module in modules:
        module_id = module.manifest.module_id
        if module_id in by_id:
            raise CatalogError(f"module registered twice: {module_id}")
        by_id[module_id] = module
    dependencies: dict[str, set[str]] = defaultdict(set)
    dependants: dict[str, set[str]] = defaultdict(set)
    for module_id, module in by_id.items():
        for required in module.manifest.requires:
            if required not in by_id:
                raise CatalogError(f"module {module_id!r} requires missing module {required!r}")
            dependencies[module_id].add(required)
            dependants[required].add(module_id)
        for optional in module.manifest.optional_requires:
            if optional in by_id:
                dependencies[module_id].add(optional)
                dependants[optional].add(module_id)
    ready = sorted(module_id for module_id in by_id if not dependencies[module_id])
    ordered: list[Module] = []
    while ready:
        module_id = ready.pop(0)
        ordered.append(by_id[module_id])
        for dependant in sorted(dependants[module_id]):
            dependencies[dependant].discard(module_id)
            if not dependencies[dependant] and by_id[dependant] not in ordered:
                ready.append(dependant)
                ready.sort()
    if len(ordered) != len(by_id):
        cyclic = sorted(module_id for module_id in by_id if dependencies[module_id])
        raise CatalogError(f"module dependency cycle: {', '.join(cyclic)}")
    return tuple(ordered)
