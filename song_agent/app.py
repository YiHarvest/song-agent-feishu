"""
FastAPI 应用模块。

创建并配置 FastAPI 应用实例，管理服务生命周期。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager

import lark_oapi as lark
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from lark_oapi.core.const import (
    LARK_REQUEST_NONCE,
    LARK_REQUEST_SIGNATURE,
    LARK_REQUEST_TIMESTAMP,
    X_REQUEST_ID,
    X_TT_LOGID,
)
from lark_oapi.core.model import RawRequest

from .api import agent_api
from .api import calendar as calendar_api
from .api import chat as chat_api
from .api import events as events_api
from .api import pending_actions as pending_actions_api
from .api import reminders as reminders_api
from .api import tasks as tasks_api
from .api.agent_auth import ApiRateLimiter
from .application.calendar_service import CalendarApplicationService
from .application.openai_adapter import OpenAIAdapter
from .application.pending_action_service import PendingActionApplicationService
from .application.reminder_service import ReminderApplicationService
from .application.request_router import RequestRouter
from .application.task_service import TaskApplicationService
from .attachments.cleanup import AttachmentCleanup
from .attachments.repository import AttachmentRepository
from .attachments.service import AttachmentService
from .attachments.tools import AttachmentTools
from .config import Settings
from .context.builders import AgentRuntimeContextBuilder, BusinessContextBuilder
from .context.service import ConversationContextService
from .executors.calendar_executor import CalendarCreateExecutor
from .executors.calendar_mutation_executor import (
    CalendarDeleteExecutor,
    CalendarUpdateExecutor,
    ReminderCancelExecutor,
)
from .executors.registry import ExecutorRegistry
from .executors.reminder_executor import ReminderCreateExecutor
from .executors.task_executor import (
    TaskCompleteExecutor,
    TaskCreateExecutor,
    TaskDeleteExecutor,
    TaskUpdateExecutor,
)
from .feishu.callbacks import FeishuCardCallbacks
from .feishu.mcp import FeishuMcp
from .feishu.media import FeishuMediaDownloader
from .feishu.oauth import FeishuOAuth
from .feishu.openapi import FeishuOpenApi
from .feishu.transport import FeishuTransport
from .intelligence.general_agent import GeneralAgent
from .intelligence.intent_extractor import IntentExtractor
from .llm import StructuredLlm
from .media.asr_client import AsrClient
from .media.vision_client import VisionClient
from .models import FeishuIdentity, IncomingMessage
from .observability.context import trace_scope
from .parsers.document_client import MinerUDocumentClient
from .planner import Planner
from .scheduler import start_scheduler
from .search.mcp import SearchMcp
from .services.api_access import ApiAccessService
from .services.audit import AuditService
from .services.encryption import AesGcmTokenCipher
from .services.outbox import ActionOutboxWorker
from .services.pending_actions import PendingActionService
from .services.reconciliation import ActionReconciliationService
from .store import SqliteStore
from .workflow import AgentWorkflow


def _lark_sdk_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Restore canonical names expected by lark-oapi's case-sensitive lookup."""
    result = dict(headers)
    lower_headers = {name.lower(): value for name, value in headers.items()}
    for name in (
        LARK_REQUEST_TIMESTAMP,
        LARK_REQUEST_NONCE,
        LARK_REQUEST_SIGNATURE,
        X_REQUEST_ID,
        X_TT_LOGID,
    ):
        if value := lower_headers.get(name.lower()):
            result[name] = value
    return result


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    创建并配置 FastAPI 应用实例。

    Args:
        settings: 可选的配置实例，未提供时从环境变量加载。

    Returns:
        配置完成的 FastAPI 应用实例。
    """
    config = settings or Settings()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
    token_cipher = AesGcmTokenCipher.from_base64_keys(
        config.token_encryption_keys,
        config.song_agent_token_active_key_version,
        bootstrap_secret=config.feishu_app_secret,
        bootstrap_context=config.feishu_app_id,
    )
    if not config.token_encryption_keys:
        logging.getLogger(__name__).warning(
            "未配置 SONG_AGENT_TOKEN_KEY_V1；当前使用飞书 App Secret 派生的兼容密钥。"
            "生产环境应配置独立的 Token 加密密钥。"
        )
    store = SqliteStore(
        config.database_path,
        app_id=config.feishu_app_id,
        token_cipher=token_cipher,
        legacy_json_path=config.data_file,
        event_retention_days=config.processed_event_retention_days,
    )
    oauth = FeishuOAuth(config, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.initialize()
        recovered_runs = await store.recover_stale_agent_runs(
            max_age_seconds=config.agent_run_timeout_seconds + 30
        )
        if recovered_runs:
            logging.getLogger(__name__).warning(
                "已将 %d 个超时未完成的 Agent run 标记为 interrupted",
                recovered_runs,
            )
        transport = FeishuTransport(config, store)
        await transport.initialize()
        attachment_tools = None
        attachment_service = None
        vision_client = None
        asr_client = None
        document_client = None
        attachment_cleanup = None
        if config.song_agent_attachments_enabled:
            attachment_repository = AttachmentRepository(store)
            vision_client = VisionClient(config)
            asr_client = AsrClient(config)
            document_client = MinerUDocumentClient(config)
            attachment_tools = AttachmentTools(
                config,
                store,
                attachment_repository,
                vision_client,
                asr_client,
                document_client,
            )
            attachment_service = AttachmentService(
                config,
                FeishuMediaDownloader(
                    transport.client,
                    timeout_seconds=config.song_agent_attachment_download_timeout_seconds,
                ),
                attachment_repository,
                attachment_tools,
            )
            await attachment_service.initialize()
            attachment_cleanup = AttachmentCleanup(
                attachment_repository,
                config.song_agent_attachment_dir,
            )
        structured_llm = StructuredLlm(config)
        planner = Planner(structured_llm, config.timezone)
        mcp = FeishuMcp(config)
        search_mcp = SearchMcp(config)
        openapi = FeishuOpenApi(config)
        pending_actions = PendingActionService(
            store,
            ttl_seconds=config.pending_action_ttl_seconds,
        )
        audit = AuditService(store)
        workflow = AgentWorkflow(
            planner,
            store,
            transport,
            oauth,
            openapi,
            mcp,
            search_mcp,
            pending_actions,
            audit,
            attachment_tools,
            attachment_service,
        )
        pending_action_service = PendingActionApplicationService(store, audit)
        calendar_service = CalendarApplicationService(
            oauth,
            pending_actions,
            openapi,
            default_timezone=config.timezone,
        )
        task_service = TaskApplicationService(oauth, pending_actions, openapi)
        reminder_service = ReminderApplicationService(calendar_service)
        business_contexts = BusinessContextBuilder(
            store,
            timezone=config.timezone,
        )
        conversation_contexts = ConversationContextService(
            store,
            structured_llm,
            business_contexts,
        )
        agent_contexts = AgentRuntimeContextBuilder()
        request_router = RequestRouter(
            IntentExtractor(structured_llm, config.timezone),
            calendar_service,
            task_service,
            reminder_service,
            pending_action_service,
            GeneralAgent(workflow.handle_general_request),
            business_contexts,
            conversation_contexts,
            agent_contexts,
        )
        workflow.set_request_router(request_router)
        api_access_service = ApiAccessService(
            store,
            api_app_id=config.song_agent_api_app_id,
            binding_code_ttl_seconds=config.song_agent_api_binding_code_ttl_seconds,
        )
        app.state.api_access_service = api_access_service
        app.state.openai_adapter = OpenAIAdapter(config, store, request_router)
        executors = ExecutorRegistry(legacy_handler=workflow.execute_pending_action)
        executors.register(
            CalendarCreateExecutor(store, oauth, openapi, audit, transport)
        )
        for executor_type in (
            CalendarUpdateExecutor,
            CalendarDeleteExecutor,
            ReminderCreateExecutor,
            ReminderCancelExecutor,
            TaskCreateExecutor,
            TaskUpdateExecutor,
            TaskCompleteExecutor,
            TaskDeleteExecutor,
        ):
            executors.register(executor_type(store, oauth, openapi, audit, transport))
        reconciliation = ActionReconciliationService(store, audit)
        outbox = ActionOutboxWorker(
            store,
            executors.execute,
            reconciliation.reconcile,
        )
        workflow.notify_outbox = outbox.notify
        pending_action_service.set_outbox_notifier(outbox.notify)
        
        async def handle_oauth_authorized(
            identity: FeishuIdentity,
            chat_id: str,
            original_request: str,
        ) -> None:
            """OAuth授权完成后的回调处理"""
            if original_request:
                # 有原始请求，自动继续处理
                await transport.send_markdown(
                    chat_id,
                    "✅ 授权完成，正在继续处理你的请求...",
                )
                # 创建一个模拟的IncomingMessage来重新触发工作流
                await workflow.enqueue(
                    IncomingMessage(
                        message_id=f"oauth_resume_{int(time.time())}",
                        event_id=f"oauth_resume_{int(time.time())}",
                        tenant_key=identity.tenant_key,
                        app_id=identity.app_id,
                        user_id=identity.subject_id,
                        open_id=identity.open_id,
                        tenant_user_id=identity.user_id,
                        union_id=identity.union_id,
                        chat_id=chat_id,
                        thread_id="",
                        root_id="",
                        chat_type="p2p",
                        message_type="text",
                        text=original_request,
                    )
                )
            else:
                # 没有原始请求，提示用户重新发送
                await transport.send_markdown(
                    chat_id,
                    "✅ 授权完成。请重新发送你的请求。",
                )
        
        oauth.on_authorized = handle_oauth_authorized
        transport.start(asyncio.get_running_loop(), workflow.enqueue)
        scheduler = await start_scheduler(config, store, transport)
        if attachment_cleanup is not None:
            scheduler.add_job(
                attachment_cleanup.run_once,
                "interval",
                hours=24,
                id="attachment-cleanup",
                name="低优先级附件清理",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
        outbox.start()
        app.state.store = store
        app.state.settings = config
        app.state.transport = transport
        app.state.oauth = oauth
        app.state.workflow = workflow
        app.state.outbox = outbox
        app.state.calendar_service = calendar_service
        app.state.task_service = task_service
        app.state.reminder_service = reminder_service
        app.state.business_contexts = business_contexts
        app.state.conversation_contexts = conversation_contexts
        app.state.pending_action_service = pending_action_service
        app.state.request_router = request_router
        app.state.attachment_service = attachment_service
        card_callbacks = FeishuCardCallbacks(
            pending_action_service,
            asyncio.get_running_loop(),
        )
        app.state.card_handler = (
            lark.EventDispatcherHandler.builder(
                config.feishu_encrypt_key,
                config.feishu_verification_token,
            )
            .register_p2_card_action_trigger(card_callbacks.handle)
            .build()
        )
        app.state.loop = asyncio.get_running_loop()
        if not config.feishu_encrypt_key:
            logging.getLogger(__name__).warning(
                "FEISHU_ENCRYPT_KEY 未配置：敏感操作确认卡片暂不可执行"
            )
        logging.getLogger(__name__).info(
            "飞书卡片回调已配置 callback_url=%s/feishu/card/action "
            "encrypt_key_configured=%s",
            config.base_url,
            bool(config.feishu_encrypt_key),
        )
        logging.getLogger(__name__).info("FastAPI 服务已启动: %s", config.base_url)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            await outbox.close()
            await transport.close(
                timeout_seconds=min(
                    max(
                        config.song_agent_vision_read_timeout_seconds + 5,
                        config.song_agent_document_parse_timeout_seconds + 5,
                    ),
                    360,
                )
            )
            if vision_client is not None:
                await vision_client.close()
            if asr_client is not None:
                await asr_client.close()
            if document_client is not None:
                await document_client.close()
            await planner.llm.close()
            await store.close()

    app = FastAPI(title="Song Agent", version="0.3.0", lifespan=lifespan)
    app.state.settings = config
    app.state.store = store
    app.state.agent_api_rate_limiter = ApiRateLimiter()
    app.include_router(oauth.router)
    app.include_router(chat_api.router)
    app.include_router(calendar_api.router)
    app.include_router(pending_actions_api.router)
    app.include_router(events_api.router)
    app.include_router(tasks_api.router)
    app.include_router(reminders_api.router)
    if config.song_agent_api_enabled:
        app.include_router(agent_api.router)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if request.url.path.startswith("/api/v1/") and isinstance(exc.detail, dict):
            content = {"error": exc.detail}
        else:
            content = {"detail": exc.detail}
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=exc.headers,
        )

    @app.middleware("http")
    async def trace_http_request(request: Request, call_next):
        with trace_scope() as trace_id:
            response = await call_next(request)
            response.headers["X-Trace-ID"] = trace_id
            return response

    @app.post("/feishu/card/action")
    async def card_action(request: Request) -> Response:
        started_at = time.monotonic()
        logger = logging.getLogger(__name__)
        if not config.feishu_encrypt_key:
            logger.error(
                "拒绝卡片回调 reason=encrypt_key_not_configured path=%s",
                request.url.path,
            )
            return Response(
                content='{"msg":"card callback encryption is not configured"}',
                status_code=503,
                media_type="application/json",
            )
        raw = RawRequest()
        raw.uri = request.url.path
        raw.headers = _lark_sdk_headers(request.headers)
        raw.body = await request.body()
        request_id = (
            request.headers.get("x-request-id")
            or request.headers.get("x-tt-logid")
            or request.headers.get("x-lark-request-id")
            or ""
        )
        logger.info(
            "收到飞书卡片回调 path=%s request_id=%s content_type=%s "
            "content_length=%s body_bytes=%d client=%s",
            request.url.path,
            request_id,
            request.headers.get("content-type", ""),
            request.headers.get("content-length", ""),
            len(raw.body),
            request.client.host if request.client else "",
        )

        try:
            card_handler = app.state.card_handler
            result = await asyncio.to_thread(card_handler.do, raw)
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log = logger.info if result.status_code < 400 else logger.error
            log(
                "飞书卡片回调完成 request_id=%s status_code=%s "
                "response_bytes=%d duration_ms=%d",
                request_id,
                result.status_code,
                len(result.content or b""),
                duration_ms,
            )
            return Response(
                content=result.content,
                status_code=result.status_code,
                media_type="application/json",
            )
        except Exception:
            logger.exception(
                "卡片回调处理失败 request_id=%s body_bytes=%d duration_ms=%d",
                request_id,
                len(raw.body),
                int((time.monotonic() - started_at) * 1000),
            )
            return Response(
                content='{"msg":"internal error"}',
                status_code=500,
                media_type="application/json",
            )

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {
            "ok": True,
            "calendar_confirmation_ready": bool(config.feishu_encrypt_key),
            "token_encryption_ready": True,
            "outbox_recovery_ready": True,
            "persistent_scheduler_ready": True,
        }

    return app


app = create_app()
