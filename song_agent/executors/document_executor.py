"""document.create / document.append 确认后的确定性执行器。"""

from __future__ import annotations

import logging
import re

from ..domain.results import ExecutionContext, ExecutionResult
from ..feishu.oauth import FeishuOAuth
from ..feishu.openapi import FeishuOpenApi
from ..feishu.transport import FeishuTransport
from ..models import DocumentBinding, FeishuIdentity, PendingAction
from ..services.audit import AuditService
from ..store import SqliteStore


class DocumentExecutor:
    """文档写操作执行器（从原 workflow 迁移，语义不变）。"""

    action_type = "document.create"

    def __init__(
        self,
        store: SqliteStore,
        oauth: FeishuOAuth,
        openapi: FeishuOpenApi,
        audit: AuditService,
        transport: FeishuTransport,
    ) -> None:
        self.store = store
        self.oauth = oauth
        self.openapi = openapi
        self.audit = audit
        self.transport = transport
        self.logger = logging.getLogger(__name__)

    async def execute(
        self,
        pending_action: PendingAction,
        context: ExecutionContext,
    ) -> ExecutionResult:
        action = pending_action
        remote_call_started = False
        try:
            if not await self.store.claim_action_execution(
                action.action_id,
                worker_id=context.worker_id,
            ):
                return ExecutionResult(
                    status="failed_final",
                    error_code="claim_failed",
                    error_message="动作未处于可执行状态",
                )
            identity = FeishuIdentity(
                tenant_key=action.tenant_key,
                app_id=action.app_id,
                open_id=action.creator_open_id,
                union_id=action.creator_subject_id,
            )
            token_context = await self.oauth.get_valid_token_context(
                identity,
                ("docx:document",),
            )
            if not token_context:
                await self.store.finish_pending_action(action.action_id, success=False)
                url = await self.oauth.create_authorization_url(
                    identity,
                    action.chat_id,
                    ("docx:document",),
                )
                await self.transport.send_markdown(
                    action.chat_id,
                    f"写入文档需要你本人授权。[点击这里完成授权]({url})，授权后再次点击原卡片。",
                )
                return ExecutionResult(
                    status="failed_final",
                    error_code="authorization_required",
                    error_message="用户授权不可用或缺少 docx:document",
                )
            title = str(action.payload.get("title") or "Agent 云文档")
            markdown = str(action.payload.get("markdown") or "")
            if not markdown:
                await self.store.expire_pending_action(action.action_id)
                await self.transport.send_markdown(action.chat_id, "文档草稿内容为空，已拒绝执行。")
                return ExecutionResult(
                    status="failed_final",
                    error_code="empty_draft",
                    error_message="文档草稿内容为空",
                )
            if action.action_type == "document.append":
                document_token = str(action.payload.get("document_token") or "")
                if not document_token:
                    await self.store.expire_pending_action(action.action_id)
                    await self.transport.send_markdown(action.chat_id, "目标文档标识缺失，已拒绝执行。")
                    return ExecutionResult(
                        status="failed_final",
                        error_code="missing_document_token",
                        error_message="目标文档标识缺失",
                    )
                remote_call_started = True
                document = await self.openapi.append_document(
                    document_token,
                    title,
                    markdown,
                    token_context,
                )
                document.url = str(action.payload.get("document_url") or document.url)
                action_text = "已追加到"
            else:
                remote_call_started = True
                document = await self.openapi.create_document(
                    title,
                    markdown,
                    token_context,
                )
                action_text = "已创建到你自己的云空间"
            await self.store.record_action_remote_success(
                action.action_id,
                remote_resource_id=document.token,
            )
            await self.store.save_document_binding(
                DocumentBinding(
                    chat_id=action.chat_id,
                    user_id=action.creator_subject_id,
                    title=document.title,
                    token=document.token,
                    url=document.url,
                ),
                tenant_key=action.tenant_key,
                app_id=action.app_id,
                thread_id=action.thread_id,
            )
            await self.store.finish_pending_action(action.action_id, success=True)
            await self.audit.record(
                action.action_type,
                "success",
                tenant_key=action.tenant_key,
                app_id=action.app_id,
                principal_id=action.creator_subject_id,
                chat_id=action.chat_id,
                thread_id=action.thread_id,
                action_id=action.action_id,
                risk_level="high",
                payload_hash=action.payload_hash,
                metadata={"remote_resource_id": document.token},
            )
            safe_title = re.sub(r"[\[\]]", "", document.title)
            await self.transport.send_markdown(
                action.chat_id,
                f"✅ {action_text}：[{safe_title}]({document.url})",
            )
            return ExecutionResult(
                status="succeeded",
                result={"remote_resource_id": document.token},
            )
        except Exception as error:
            if remote_call_started:
                await self.store.mark_action_unknown(
                    action.action_id,
                    error_code="document_remote_result_uncertain",
                    error_message=str(error),
                )
            else:
                await self.store.finish_pending_action(action.action_id, success=False)
            await self.audit.record(
                action.action_type,
                "failure",
                tenant_key=action.tenant_key,
                app_id=action.app_id,
                principal_id=action.creator_subject_id,
                chat_id=action.chat_id,
                thread_id=action.thread_id,
                action_id=action.action_id,
                risk_level="high",
                payload_hash=action.payload_hash,
            )
            self.logger.exception("执行待确认文档动作失败 action_id=%s", action.action_id)
            await self.transport.send_markdown(
                action.chat_id,
                (
                    "文档请求的远端结果暂时无法确认，已停止自动重试并进入核对队列。"
                    if remote_call_started
                    else "文档写入失败，详细原因已写入日志；该草稿可安全重试。"
                ),
            )
            return ExecutionResult(
                status="failed_final" if remote_call_started else "failed_retryable",
                error_code=(
                    "document_remote_result_uncertain" if remote_call_started else "document_execution_error"
                ),
                error_message=str(error),
            )


class DocumentAppendExecutor(DocumentExecutor):
    """document.append 确认后的执行器。"""

    action_type = "document.append"
