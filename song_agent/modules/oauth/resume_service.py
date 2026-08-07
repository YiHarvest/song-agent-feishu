"""OAuth 完成后恢复原始请求（Q10）。

不再伪造 IncomingMessage 重入消息泵；直接构造 UserRequest 走
`ApplicationDispatcher.resume()`，输出走 `FeishuResponsePresenter`。
恢复时重新执行 IntentExtractor（不持久化意图结果）；不重复 record_user。
"""

from __future__ import annotations

import hashlib
import logging

from ...application.dispatcher import ApplicationDispatcher
from ...channels.feishu.response_presenter import FeishuResponsePresenter
from ...domain.intents import UserRequest
from ...models import FeishuIdentity
from ...store import SqliteStore
from .resume_models import OAuthResumeRequest, parse_resume_payload


class OAuthResumeService:
    def __init__(
        self,
        dispatcher: ApplicationDispatcher,
        presenter: FeishuResponsePresenter,
        store: SqliteStore,
        *,
        app_id: str = "",
    ) -> None:
        self.dispatcher = dispatcher
        self.presenter = presenter
        self.store = store
        self.app_id = app_id
        self.logger = logging.getLogger(__name__)

    async def resume(
        self,
        identity: FeishuIdentity,
        chat_id: str,
        original_request: str,
    ) -> None:
        """授权回调触发：恢复原请求并继续处理。"""
        resume = parse_resume_payload(original_request)
        context = await self._build_context(resume, identity)
        request = UserRequest(
            identity=resume.to_identity() if resume.tenant_key else identity,
            text=resume.text,
            source=resume.source or "feishu",
            chat_id=resume.chat_id or chat_id,
            thread_id=resume.thread_id or resume.root_id,
            message_id=resume.original_message_id,
            event_id=resume.original_event_id,
            context=context,
        )
        authorization_id = self._authorization_id(identity, chat_id, resume)
        try:
            result = await self.dispatcher.resume(
                request,
                authorization_id=authorization_id,
            )
            await self.presenter.present_resumed(
                chat_id,
                request.identity,
                result,
            )
        except Exception:
            self.logger.exception(
                "OAuth 恢复执行失败 chat=%s principal=%s",
                chat_id,
                identity.subject_id,
            )
            await self.presenter.present_resumed(
                chat_id,
                request.identity,
                _failure_result("授权成功，但原请求恢复失败。请重新发送你的请求。"),
            )

    async def _build_context(
        self,
        resume: OAuthResumeRequest,
        identity: FeishuIdentity,
    ) -> dict:
        """按引用恢复附件解析上下文（重新校验归属与有效期）。"""
        retrieved: list[dict] = []
        for result_ref in resume.retrieved_result_refs:
            result = await self.store.get_tool_result(
                result_ref,
                tenant_key=resume.tenant_key or identity.tenant_key,
                app_id=resume.app_id or identity.app_id,
                principal_id=resume.principal_id or identity.subject_id,
            )
            if result is not None:
                retrieved.append(result)
        if retrieved:
            return {"retrieved_context": retrieved}
        return {}

    def _authorization_id(
        self,
        identity: FeishuIdentity,
        chat_id: str,
        resume: OAuthResumeRequest,
    ) -> str:
        raw = ":".join(
            (
                identity.tenant_key,
                identity.app_id,
                identity.subject_id,
                chat_id,
                resume.text,
            )
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _failure_result(message: str):
    from ...domain.results import ApplicationResult

    return ApplicationResult(status="error", intent="conversation.general", message=message)
