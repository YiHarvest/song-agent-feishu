from __future__ import annotations

from collections.abc import Iterable

from ..kernel.errors import CatalogError
from ..kernel.ids import (
    RESERVED_INTENT_NAMESPACES,
    top_level_namespace,
    validate_intent_id,
    validate_module_id,
)
from ..kernel.intent import (
    IdempotencyPolicy,
    IntentSpec,
    OperationKind,
    RiskLevel,
    UnknownResolutionPolicy,
)
from ..kernel.models import IntentResult
from ..ports.llm import AgentToolDescriptor, IntentDescriptor


class IntentCatalog:
    def __init__(self) -> None:
        self._specs: dict[str, IntentSpec] = {}
        self._module_namespaces: dict[str, frozenset[str]] = {}

    def declare_module(self, module_id: str, namespaces: frozenset[str]) -> None:
        module_id = validate_module_id(module_id)
        if module_id in self._module_namespaces:
            raise CatalogError(f"module already declared: {module_id}")
        normalized = frozenset(namespace.strip() for namespace in namespaces)
        for namespace in normalized:
            if not namespace or "." in namespace:
                raise CatalogError(f"invalid top-level namespace: {namespace!r}")
            owner = next(
                (
                    candidate
                    for candidate, owned in self._module_namespaces.items()
                    if namespace in owned
                ),
                None,
            )
            if owner is not None:
                raise CatalogError(
                    f"namespace {namespace!r} owned by both {owner!r} and {module_id!r}"
                )
            if namespace in RESERVED_INTENT_NAMESPACES and module_id != namespace:
                raise CatalogError(
                    f"reserved namespace {namespace!r} must be owned by module {namespace!r}"
                )
        self._module_namespaces[module_id] = normalized

    def register(self, spec: IntentSpec) -> None:
        intent_id = validate_intent_id(spec.intent_id)
        if intent_id in self._specs:
            raise CatalogError(f"intent already registered: {intent_id}")
        if spec.module_id not in self._module_namespaces:
            raise CatalogError(f"intent module was not declared: {spec.module_id}")
        namespace = top_level_namespace(intent_id)
        if namespace not in self._module_namespaces[spec.module_id]:
            raise CatalogError(
                f"module {spec.module_id!r} does not own intent namespace {namespace!r}"
            )
        if spec.version < 1:
            raise CatalogError(f"intent version must be positive: {intent_id}")
        if spec.execution_timeout_seconds <= 0:
            raise CatalogError(f"intent timeout must be positive: {intent_id}")
        if spec.result_model is None:
            raise CatalogError(f"intent must declare a result model: {intent_id}")
        if not issubclass(spec.result_model, IntentResult):
            raise CatalogError(
                f"intent result model must use the channel-neutral IntentResult contract: {intent_id}"
            )
        if spec.is_write and spec.idempotency_policy is not IdempotencyPolicy.REQUIRED:
            raise CatalogError(f"write intent must require idempotency: {intent_id}")
        if spec.operation_kind in {
            OperationKind.DELETE,
            OperationKind.OVERWRITE,
            OperationKind.BULK,
        } and spec.risk_level is not RiskLevel.HIGH:
            raise CatalogError(f"destructive intent must declare high risk: {intent_id}")
        if spec.unknown_resolution_policy in {
            UnknownResolutionPolicy.RECONCILE,
            UnknownResolutionPolicy.RECONCILE_THEN_RETRY,
        } and spec.reconcile is None:
            raise CatalogError(f"intent declares reconciliation without a handler: {intent_id}")
        if not spec.is_write and spec.unknown_resolution_policy is not UnknownResolutionPolicy.NONE:
            raise CatalogError(f"read intent cannot declare UNKNOWN resolution: {intent_id}")
        self._specs[intent_id] = spec

    def resolve(self, intent_id: str) -> IntentSpec:
        normalized = validate_intent_id(intent_id)
        try:
            return self._specs[normalized]
        except KeyError as error:
            raise CatalogError(f"intent is not registered: {normalized}") from error

    def all(self) -> tuple[IntentSpec, ...]:
        return tuple(self._specs[key] for key in sorted(self._specs))

    def top_level_descriptors(self) -> tuple[IntentDescriptor, ...]:
        return tuple(
            IntentDescriptor(
                intent_id=spec.intent_id,
                description=spec.description,
                arguments_schema=spec.arguments_model.model_json_schema(),
                examples=spec.examples,
            )
            for spec in self.all()
            if spec.top_level_exposed
        )

    def agent_tools(self) -> tuple[AgentToolDescriptor, ...]:
        return tuple(
            AgentToolDescriptor(
                intent_id=spec.intent_id,
                description=spec.description,
                arguments_schema=spec.arguments_model.model_json_schema(),
            )
            for spec in self.all()
            if spec.agent_exposed and spec.intent_id != "agent.execute"
        )

    def __contains__(self, intent_id: object) -> bool:
        return isinstance(intent_id, str) and intent_id in self._specs

    def __len__(self) -> int:
        return len(self._specs)


def validate_catalog_complete(catalog: IntentCatalog, required: Iterable[str]) -> None:
    missing = sorted(set(required) - {spec.intent_id for spec in catalog.all()})
    if missing:
        raise CatalogError(f"required intents are missing: {', '.join(missing)}")
