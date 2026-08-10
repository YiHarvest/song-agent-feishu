from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..kernel.intent import IntentSpec
from ..kernel.models import ExecutionContext, IntentResult
from ..kernel.module import ContributionSink, ModuleManifest
from ..ports.search import SearchPort
from ..runtime.providers import ProviderRegistry


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=10_000)
    provider: str = "auto"
    max_results: int = Field(default=5, ge=1, le=20)


class SearchHandler:
    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        args = SearchArguments.model_validate(arguments)
        provider: SearchPort = self.providers.get(
            SearchPort, args.provider if args.provider != "auto" else None
        )
        hits = await provider.search(
            args.query,
            provider=args.provider,
            max_results=args.max_results,
        )
        return IntentResult.success(
            code="search.completed",
            data={
                "query": args.query,
                "hits": [
                    {
                        "title": hit.title,
                        "url": hit.url,
                        "snippet": hit.snippet,
                        "source": hit.source,
                    }
                    for hit in hits
                ],
            },
            kind="list",
        )


class SearchModule:
    manifest = ModuleManifest(
        module_id="search", version="1.0.0", owns_namespaces=frozenset({"search"})
    )

    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    def contribute(self, sink: ContributionSink) -> None:
        sink.add_intent(
            IntentSpec(
                intent_id="search.query",
                version=1,
                module_id="search",
                description="Search external information sources.",
                arguments_model=SearchArguments,
                result_model=IntentResult,
                handler=SearchHandler(self.providers),
                agent_exposed=True,
            )
        )
