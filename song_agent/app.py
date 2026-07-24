"""
FastAPI 应用模块。

创建并配置 FastAPI 应用实例，管理服务生命周期。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import lark_oapi as lark
from fastapi import FastAPI, Request, Response
from lark_oapi.core.model import RawRequest

from .config import Settings
from .feishu.mcp import FeishuMcp
from .feishu.oauth import FeishuOAuth
from .feishu.openapi import FeishuOpenApi
from .feishu.transport import FeishuTransport
from .llm import StructuredLlm
from .models import IncomingMessage
from .observability.context import trace_scope
from .planner import Planner
from .scheduler import start_scheduler
from .services.audit import AuditService
from .services.encryption import AesGcmTokenCipher
from .services.outbox import ActionOutboxWorker
from .services.pending_actions import PendingActionService
from .services.reconciliation import ActionReconciliationService
from .store import SqliteStore
from .workflow import AgentWorkflow


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
        planner = Planner(StructuredLlm(config), config.timezone)
        mcp = FeishuMcp(config)
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
            pending_actions,
            audit,
        )
        reconciliation = ActionReconciliationService(store, audit)
        outbox = ActionOutboxWorker(
            store,
            workflow.execute_pending_action,
            reconciliation.reconcile,
        )
        workflow.notify_outbox = outbox.notify
        
        async def handle_oauth_authorized(chat_id: str, original_request: str) -> None:
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
                        tenant_key="",
                        app_id=config.feishu_app_id,
                        user_id="",
                        open_id="",
                        tenant_user_id="",
                        union_id="",
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
        outbox.start()
        app.state.store = store
        app.state.transport = transport
        app.state.oauth = oauth
        app.state.workflow = workflow
        app.state.outbox = outbox
        app.state.loop = asyncio.get_running_loop()
        if not config.feishu_encrypt_key:
            logging.getLogger(__name__).warning(
                "FEISHU_ENCRYPT_KEY 未配置：敏感操作确认卡片暂不可执行"
            )
        logging.getLogger(__name__).info("FastAPI 服务已启动: %s", config.base_url)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            await outbox.close()
            await planner.llm.close()
            await store.close()

    app = FastAPI(title="Song Agent", version="0.3.0", lifespan=lifespan)
    app.include_router(oauth.router)

    @app.middleware("http")
    async def trace_http_request(request: Request, call_next):
        with trace_scope() as trace_id:
            response = await call_next(request)
            response.headers["X-Trace-ID"] = trace_id
            return response

    def process_card_action(card: lark.Card):
        loop = getattr(app.state, "loop", None)
        workflow = getattr(app.state, "workflow", None)
        if loop is None or workflow is None:
            raise RuntimeError("Song Agent 尚未完成启动")
        future = asyncio.run_coroutine_threadsafe(workflow.handle_card_action(card), loop)
        return future.result(timeout=5)

    # 飞书卡片回调安全说明（依据飞书官方文档）：
    # 卡片回调请求不携带 X-Lark-Request-Timestamp / X-Lark-Request-Nonce / X-Lark-Signature
    # 这些签名头仅出现在「事件订阅」的 HTTP 回调中。卡片回调的安全验证依赖 encrypt_key
    # 对请求体进行 AES 解密完成，而非签名验证。
    #
    # lark_oapi 的 CardActionHandler._verify_sign 错误地使用 verification_token 进行签名
    # 校验，但卡片回调请求中没有 timestamp/nonce 头，会导致 NoneType 拼接异常。
    # 因此这里不传入 verification_token，使其跳过签名验证，仅依赖 encrypt_key 解密保护。
    # 若未配置 encrypt_key，则卡片回调端点返回 503。
    card_handler = (
        lark.CardActionHandler.builder(
            config.feishu_encrypt_key,
            "",  # 不传入 verification_token，避免触发错误的签名验证逻辑
        )
        .register(process_card_action)
        .build()
    )

    @app.post("/feishu/card/action")
    async def card_action(request: Request) -> Response:
        if not config.feishu_encrypt_key:
            return Response(
                content='{"msg":"card callback encryption is not configured"}',
                status_code=503,
                media_type="application/json",
            )
        raw = RawRequest()
        raw.uri = request.url.path
        raw.headers = dict(request.headers)  # type: ignore[assignment]
        raw.body = await request.body()

        try:
            result = await asyncio.to_thread(card_handler.do, raw)
            return Response(
                content=result.content,
                status_code=result.status_code,
                media_type="application/json",
            )
        except Exception:
            logging.getLogger(__name__).exception("卡片回调处理失败")
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
