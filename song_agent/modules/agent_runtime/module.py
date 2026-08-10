from __future__ import annotations

from ...kernel.intent import ContextRequirement, IntentSpec
from ...kernel.models import IntentResult
from ...kernel.module import ContributionSink, ModuleManifest
from ...ports.llm import AgentModelPort
from ...runtime.agent import AgentExecuteArguments, AgentHandler
from ...runtime.catalog import IntentCatalog


class AgentModule:
    manifest = ModuleManifest(
        module_id="agent",
        version="1.0.0",
        owns_namespaces=frozenset({"agent"}),
        optional_requires=("conversation", "memory"),
    )

    def __init__(self, *, catalog: IntentCatalog, model: AgentModelPort, max_steps: int = 8) -> None:
        self.handler = AgentHandler(catalog=catalog, model=model, max_steps=max_steps)

    def contribute(self, sink: ContributionSink) -> None:
        sink.add_intent(
            IntentSpec(
                intent_id="agent.execute",
                version=1,
                module_id="agent",
                description="Execute a complex multi-step request using several registered capabilities.",
                arguments_model=AgentExecuteArguments,
                result_model=IntentResult,
                handler=self.handler,
                top_level_exposed=True,
                agent_exposed=False,
                context_requirements=(
                    ContextRequirement("conversation.recent", required=False, max_chars=6000),
                    ContextRequirement("conversation.summary", required=False, max_chars=3000),
                    ContextRequirement("memory.long_term", required=False, max_chars=3000),
                ),
                execution_timeout_seconds=180,
                examples=("搜索资料后比较并总结", "查询日程并创建一个相关任务"),
            )
        )
