from __future__ import annotations

import pytest
from pydantic import BaseModel

from song_agent.kernel.errors import CatalogError
from song_agent.kernel.intent import IntentSpec
from song_agent.kernel.models import IntentResult
from song_agent.kernel.module import ModuleManifest
from song_agent.runtime.catalog import IntentCatalog
from song_agent.runtime.context import ContextAssembler
from song_agent.runtime.modules import ModuleRuntime
from song_agent.runtime.providers import ProviderRegistry


class EmptyArguments(BaseModel):
    pass


async def handler(context, arguments):
    return IntentResult.success(code="test.ok")


class SampleModule:
    def __init__(self, module_id: str, namespace: str, requires: tuple[str, ...] = ()) -> None:
        self.manifest = ModuleManifest(
            module_id=module_id,
            version="1",
            owns_namespaces=frozenset({namespace}),
            requires=requires,
        )

    def contribute(self, sink) -> None:
        sink.add_intent(
            IntentSpec(
                intent_id=f"{next(iter(self.manifest.owns_namespaces))}.run",
                version=1,
                module_id=self.manifest.module_id,
                description="test",
                arguments_model=EmptyArguments,
                result_model=IntentResult,
                handler=handler,
            )
        )


def runtime(*modules: SampleModule) -> ModuleRuntime:
    return ModuleRuntime(
        modules,
        catalog=IntentCatalog(),
        providers=ProviderRegistry(),
        contexts=ContextAssembler(),
    )


def test_module_dependency_order_and_catalog_source_of_truth() -> None:
    result = runtime(
        SampleModule("beta", "beta", requires=("alpha",)),
        SampleModule("alpha", "alpha"),
    )
    result.assemble()
    assert [module.manifest.module_id for module in result.modules] == ["alpha", "beta"]
    assert [spec.intent_id for spec in result.catalog.all()] == ["alpha.run", "beta.run"]
    assert [item.intent_id for item in result.catalog.top_level_descriptors()] == [
        "alpha.run",
        "beta.run",
    ]


def test_namespace_conflicts_and_cycles_fail_startup() -> None:
    with pytest.raises(CatalogError, match="owned by both"):
        runtime(SampleModule("alpha", "same"), SampleModule("beta", "same")).assemble()
    with pytest.raises(CatalogError, match="cycle"):
        runtime(
            SampleModule("alpha", "alpha", requires=("beta",)),
            SampleModule("beta", "beta", requires=("alpha",)),
        )


def test_reserved_namespace_requires_matching_core_module() -> None:
    with pytest.raises(CatalogError, match="reserved namespace"):
        runtime(SampleModule("plugin", "agent")).assemble()


def test_catalog_rejects_channel_specific_result_contract() -> None:
    class ChannelResult(BaseModel):
        card_json: dict

    module = SampleModule("alpha", "alpha")
    result = runtime(module)
    result.catalog.declare_module("alpha", frozenset({"alpha"}))
    with pytest.raises(CatalogError, match="channel-neutral IntentResult"):
        result.catalog.register(
            IntentSpec(
                intent_id="alpha.channel_result",
                version=1,
                module_id="alpha",
                description="invalid channel result",
                arguments_model=EmptyArguments,
                result_model=ChannelResult,
                handler=handler,
            )
        )
