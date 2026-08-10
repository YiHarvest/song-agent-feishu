from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..adapters.attachment_understanding import (
    HttpAsrProvider,
    HttpDocumentParserProvider,
    OpenAIVisionProvider,
)
from ..adapters.attachments import LocalAttachmentStore
from ..adapters.auth import ApiKeyAuthenticator, SemanticAuthorization
from ..adapters.encryption import SingleKeyCipher
from ..adapters.feishu_channel import FeishuChannelAdapter
from ..adapters.feishu_oauth import FeishuOAuthService
from ..adapters.feishu_workspace import FeishuDocumentProvider, FeishuWorkspaceProvider
from ..adapters.openai_llm import OpenAIModelAdapter
from ..adapters.providers import (
    UnavailableAttachmentUnderstandingProvider,
    UnavailableSearchProvider,
    UnavailableWorkspaceProvider,
)
from ..adapters.rate_limit import FixedWindowRateLimiter
from ..adapters.search import TavilySearchProvider
from ..config import Settings
from ..infrastructure.database import Database
from ..infrastructure.repositories import (
    SqliteActionRepository,
    SqliteAttachmentRepository,
    SqliteAuditRepository,
    SqliteConversationRepository,
    SqliteDeliveryBindingRepository,
    SqliteEventDedupRepository,
    SqliteExecutionRepository,
    SqliteMemoryRepository,
    SqliteModuleRecordRepository,
    SqliteOAuthCredentialRepository,
    SqliteSchedulerLeaseRepository,
)
from ..kernel.module import ModuleManifest, ProviderContribution
from ..modules.agent_runtime import AgentModule
from ..modules.attachment_module import AttachmentModule
from ..modules.conversation import ConversationModule
from ..modules.delivery import DeliveryModule
from ..modules.memory import MemoryModule
from ..modules.plan import PlanModule
from ..modules.search_module import SearchModule
from ..modules.system_module import SystemModule
from ..modules.workspace import CalendarModule, DocumentModule, ReminderModule, TaskModule
from ..ports.attachments import AsrPort, AttachmentStorePort, DocumentParserPort, VisionPort
from ..ports.search import SearchPort
from ..ports.workspace import CalendarPort, DocumentPort, TaskPort
from ..runtime.actions import ActionService
from ..runtime.catalog import IntentCatalog
from ..runtime.channels import ChannelRegistry
from ..runtime.context import ContextAssembler
from ..runtime.coordinator import ExecutionCoordinator
from ..runtime.dispatcher import IntentDispatcher
from ..runtime.modules import ModuleRuntime, discover_external_modules
from ..runtime.providers import ProviderRegistry
from ..runtime.risk import RiskPolicy
from ..runtime.router import TopLevelRouter
from ..runtime.scheduler import SchedulerService


class ProviderModule:
    manifest = ModuleManifest(
        module_id="providers",
        version="1.0.0",
        owns_namespaces=frozenset(),
    )

    def __init__(
        self,
        workspace: object,
        document: object,
        search: object,
        attachment_store: object,
        vision: object,
        asr: object,
        document_parser: object,
        workspace_provider_id: str,
        search_provider_id: str,
    ) -> None:
        self.workspace = workspace
        self.document = document
        self.search = search
        self.attachment_store = attachment_store
        self.vision = vision
        self.asr = asr
        self.document_parser = document_parser
        self.workspace_provider_id = workspace_provider_id
        self.search_provider_id = search_provider_id

    def contribute(self, sink: object) -> None:
        sink.add_provider(
            ProviderContribution(
                CalendarPort, self.workspace_provider_id, self.workspace, True
            )
        )
        sink.add_provider(
            ProviderContribution(TaskPort, self.workspace_provider_id, self.workspace, True)
        )
        sink.add_provider(
            ProviderContribution(DocumentPort, self.workspace_provider_id, self.document, True)
        )
        sink.add_provider(
            ProviderContribution(SearchPort, self.search_provider_id, self.search, True)
        )
        sink.add_provider(
            ProviderContribution(AttachmentStorePort, "local", self.attachment_store, True)
        )
        sink.add_provider(ProviderContribution(VisionPort, "default", self.vision, True))
        sink.add_provider(ProviderContribution(AsrPort, "default", self.asr, True))
        sink.add_provider(
            ProviderContribution(DocumentParserPort, "default", self.document_parser, True)
        )


@dataclass(slots=True)
class ApplicationRuntime:
    settings: Settings
    database: Database
    modules: ModuleRuntime
    catalog: IntentCatalog
    dispatcher: IntentDispatcher
    router: TopLevelRouter
    actions: ActionService
    authenticator: ApiKeyAuthenticator
    delivery_bindings: SqliteDeliveryBindingRepository
    coordinator: ExecutionCoordinator
    model: OpenAIModelAdapter
    feishu_channel: FeishuChannelAdapter | None
    feishu_oauth: FeishuOAuthService | None
    scheduler: SchedulerService
    rate_limiter: FixedWindowRateLimiter

    async def start(self) -> None:
        await self.database.verify_schema()
        await self.modules.start()

    async def close(self) -> None:
        await self.modules.close()
        await self.model.close()


def build_runtime(settings: Settings) -> ApplicationRuntime:
    cipher = SingleKeyCipher(settings.core.master_key.get_secret_value())
    database = Database(settings.core.database_path)
    executions = SqliteExecutionRepository(database)
    actions = SqliteActionRepository(database)
    audit = SqliteAuditRepository(database)
    conversations = SqliteConversationRepository(database)
    memories = SqliteMemoryRepository(database)
    module_records = SqliteModuleRecordRepository(database)
    attachments = SqliteAttachmentRepository(database)
    bindings = SqliteDeliveryBindingRepository(database)
    event_dedup = SqliteEventDedupRepository(database)
    scheduler_leases = SqliteSchedulerLeaseRepository(database)
    oauth_credentials = SqliteOAuthCredentialRepository(database, cipher)

    model = OpenAIModelAdapter(
        base_url=str(settings.llm.base_url).rstrip("/"),
        api_key=settings.llm.api_key.get_secret_value(),
        model=settings.llm.model,
        connect_timeout=settings.llm.connect_timeout_seconds,
        read_timeout=settings.llm.read_timeout_seconds,
        max_retries=settings.llm.max_retries,
    )
    catalog = IntentCatalog()
    providers = ProviderRegistry()
    contexts = ContextAssembler()
    channels = ChannelRegistry()
    workspace_modules = settings.core.module_ids & {
        "calendar",
        "task",
        "reminder",
        "document",
    }
    feishu_oauth: FeishuOAuthService | None = None
    if settings.feishu.workspace_enabled:
        if not settings.feishu.app_id or settings.feishu.app_secret is None:
            raise ValueError(
                "FEISHU_WORKSPACE_ENABLED requires FEISHU_APP_ID and FEISHU_APP_SECRET"
            )
        feishu_oauth = FeishuOAuthService(
            app_id=settings.feishu.app_id,
            app_secret=settings.feishu.app_secret.get_secret_value(),
            base_url=str(settings.feishu.domain),
            public_base_url=str(settings.core.public_base_url),
            repository=oauth_credentials,
            permission_scope_map=SemanticAuthorization.FEISHU_SCOPE_MAP,
        )
    if workspace_modules:
        if feishu_oauth is None:
            raise ValueError(
                "enabled workspace modules require FEISHU_WORKSPACE_ENABLED"
            )
        workspace_provider = FeishuWorkspaceProvider(
            base_url=str(settings.feishu.domain),
            token_provider=feishu_oauth,
            calendar_id=settings.feishu.calendar_id,
        )
        document_provider: object = FeishuDocumentProvider(workspace_provider)
        workspace_provider_id = "feishu"
    else:
        workspace_provider = UnavailableWorkspaceProvider("feishu")
        document_provider = workspace_provider
        workspace_provider_id = "unavailable"
    attachment_store = LocalAttachmentStore(
        root=settings.attachments.directory,
        repository=attachments,
        max_bytes=settings.attachments.max_bytes,
        ttl_seconds=settings.attachments.ttl_seconds,
    )
    if "attachment" in settings.core.module_ids:
        if (
            settings.attachments.vision_base_url is None
            or settings.attachments.vision_api_key is None
            or not settings.attachments.vision_model
            or settings.attachments.asr_url is None
            or settings.attachments.document_parser_url is None
        ):
            raise ValueError(
                "the attachment module requires vision, ASR and document parser provider settings"
            )
        vision_provider: object = OpenAIVisionProvider(
            base_url=str(settings.attachments.vision_base_url),
            api_key=settings.attachments.vision_api_key.get_secret_value(),
            model=settings.attachments.vision_model,
        )
        asr_provider: object = HttpAsrProvider(str(settings.attachments.asr_url))
        document_parser_provider: object = HttpDocumentParserProvider(
            str(settings.attachments.document_parser_url)
        )
    else:
        unavailable_understanding = UnavailableAttachmentUnderstandingProvider()
        vision_provider = unavailable_understanding
        asr_provider = unavailable_understanding
        document_parser_provider = unavailable_understanding
    if "search" in settings.core.module_ids:
        if (
            not settings.search.enabled
            or settings.search.provider != "tavily"
            or settings.search.tavily_api_key is None
        ):
            raise ValueError(
                "the search module requires SONG_AGENT_SEARCH_ENABLED=true, "
                "provider=tavily and SONG_AGENT_SEARCH_TAVILY_API_KEY"
            )
        search_provider: object = TavilySearchProvider(
            api_key=settings.search.tavily_api_key.get_secret_value(),
            endpoint=str(settings.search.tavily_endpoint),
        )
        search_provider_id = "tavily"
    else:
        search_provider = UnavailableSearchProvider()
        search_provider_id = "unavailable"
    builtin_modules = {
        "conversation": ConversationModule(model=model, repository=conversations),
        "delivery": DeliveryModule(bindings),
        "memory": MemoryModule(memories),
        "agent": AgentModule(catalog=catalog, model=model),
        "plan": PlanModule(module_records),
        "calendar": CalendarModule(providers),
        "task": TaskModule(providers),
        "reminder": ReminderModule(providers),
        "document": DocumentModule(providers),
        "search": SearchModule(providers),
        "attachment": AttachmentModule(providers),
        "system": SystemModule(channels),
    }
    selected = [
        ProviderModule(
            workspace_provider,
            document_provider,
            search_provider,
            attachment_store,
            vision_provider,
            asr_provider,
            document_parser_provider,
            workspace_provider_id,
            search_provider_id,
        )
    ]
    unknown = settings.core.module_ids - builtin_modules.keys()
    if unknown:
        raise ValueError(f"unknown enabled modules: {', '.join(sorted(unknown))}")
    selected.extend(
        module for module_id, module in builtin_modules.items() if module_id in settings.core.module_ids
    )
    selected.extend(
        discover_external_modules(
            enabled=settings.core.external_plugins_enabled,
            allowlist=settings.core.plugin_allowlist,
        )
    )
    module_runtime = ModuleRuntime(
        tuple(selected),
        catalog=catalog,
        providers=providers,
        contexts=contexts,
    )
    module_runtime.assemble()
    dispatcher = IntentDispatcher(
        catalog=catalog,
        authorization=SemanticAuthorization(feishu_oauth),
        executions=executions,
        actions=actions,
        audit=audit,
        contexts=contexts,
        risk_policy=RiskPolicy(confirm_all_writes=settings.core.confirm_all_writes),
        module_versions=module_runtime.module_versions,
        max_execution_timeout_seconds=settings.core.max_execution_timeout_seconds,
    )
    agent_module = builtin_modules["agent"]
    if "agent" in settings.core.module_ids:
        agent_module.handler.bind_dispatcher(dispatcher)
    router = TopLevelRouter(catalog, model, contexts)
    action_service = ActionService(actions, dispatcher, catalog, executions)
    coordinator = ExecutionCoordinator()
    feishu_channel: FeishuChannelAdapter | None = None
    if settings.feishu.channel_enabled:
        if (
            not settings.feishu.app_id
            or settings.feishu.app_secret is None
            or settings.feishu.verification_token is None
        ):
            raise ValueError(
                "FEISHU_CHANNEL_ENABLED requires FEISHU_APP_ID, "
                "FEISHU_APP_SECRET and FEISHU_VERIFICATION_TOKEN"
            )
        feishu_channel = FeishuChannelAdapter(
            app_id=settings.feishu.app_id,
            app_secret=settings.feishu.app_secret.get_secret_value(),
            base_url=str(settings.feishu.domain),
            verification_token=settings.feishu.verification_token.get_secret_value(),
            encrypt_key=(
                settings.feishu.encrypt_key.get_secret_value()
                if settings.feishu.encrypt_key
                else ""
            ),
            permissions=settings.feishu.permissions,
            router=router,
            dispatcher=dispatcher,
            actions=action_service,
            dedup=event_dedup,
            attachments=attachment_store,
            coordinator=coordinator,
        )
        channels.register(feishu_channel)
    scheduler = SchedulerService(
        owner_id=f"runtime-{uuid.uuid4().hex}",
        leases=scheduler_leases,
        dispatcher=dispatcher,
        lease_seconds=settings.scheduler.lease_seconds,
    )
    return ApplicationRuntime(
        settings=settings,
        database=database,
        modules=module_runtime,
        catalog=catalog,
        dispatcher=dispatcher,
        router=router,
        actions=action_service,
        authenticator=ApiKeyAuthenticator(
            api_key=settings.api.key.get_secret_value(),
            tenant_id=settings.api.default_tenant,
            principal_id=settings.api.principal_id,
        ),
        delivery_bindings=bindings,
        coordinator=coordinator,
        model=model,
        feishu_channel=feishu_channel,
        feishu_oauth=feishu_oauth,
        scheduler=scheduler,
        rate_limiter=FixedWindowRateLimiter(settings.api.rate_limit_per_minute),
    )
