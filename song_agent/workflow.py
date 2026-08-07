"""
Agent 工作流模块。

处理消息分发、意图识别、计划创建、文档生成等核心业务逻辑。
支持自然语言交互，自动识别用户意图。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .agent.context import AgentContext
from .agent.models import AgentResult, ToolResult
from .agent.runtime import ReActRuntime
from .agent.tool_registry import AgentTool, ToolRegistry
from .application.dispatcher import ApplicationDispatcher
from .application.result_renderer import render_message
from .attachments.models import (
    AnalyzeImageInput,
    AttachmentAccess,
    ParseDocumentInput,
    TranscribeAudioInput,
)
from .attachments.service import AttachmentService
from .attachments.tools import AttachmentTools
from .channels.feishu.command_router import FeishuCommandRouter
from .channels.feishu.response_presenter import FeishuResponsePresenter
from .domain.intents import UserRequest
from .domain.results import ApplicationResult
from .feishu.media import FeishuMediaPermissionError
from .feishu.oauth import FeishuOAuth
from .feishu.openapi import FeishuOpenApi
from .feishu.transport import FeishuTransport, clean_incoming_text
from .media.vision_client import (
    VisionBusyError,
    VisionConnectionError,
    VisionTimeoutError,
)
from .models import (
    DailyRecord,
    DailyReview,
    DocumentOutput,
    IncomingMessage,
    PendingAction,
    PlanOutput,
    ReviewOutput,
    ReviewUpdate,
)
from .observability.context import trace_scope
from .planner import (
    Planner,
    apply_review,
    build_tasks,
    format_plan,
    format_review,
    has_explicit_time_hint,
)
from .policies.tool_policy import ToolPolicyGuard
from .search.mcp import SearchMcp
from .services.agent_runs import AgentRunRecorder
from .services.audit import AuditService
from .services.pending_actions import PendingActionService
from .store import SqliteStore


class AgentWorkflow:
    """
    Agent 工作流处理器。

    处理消息分发、意图识别、计划创建、文档生成等核心业务逻辑。
    支持自然语言交互，自动识别用户意图。
    """

    def __init__(
        self,
        planner: Planner,
        store: SqliteStore,
        transport: FeishuTransport,
        oauth: FeishuOAuth,
        openapi: FeishuOpenApi,
        search_mcp: SearchMcp,
        pending_actions: PendingActionService,
        audit: AuditService,
        attachment_tools: AttachmentTools | None = None,
        attachment_service: AttachmentService | None = None,
        presenter: FeishuResponsePresenter | None = None,
        command_router: FeishuCommandRouter | None = None,
    ) -> None:
        self.planner = planner
        self.store = store
        self.transport = transport
        self.oauth = oauth
        self.openapi = openapi
        self.search_mcp = search_mcp
        self.pending_actions = pending_actions
        self.audit = audit
        self.attachment_tools = attachment_tools
        self.attachment_service = attachment_service
        self.presenter = presenter
        self.command_router = command_router
        self.logger = logging.getLogger(__name__)
        self._locks: dict[str, asyncio.Lock] = {}
        self.dispatcher: ApplicationDispatcher | None = None
        self.tools = self._build_tool_registry()
        settings = self.oauth.settings
        self.agent_runs = AgentRunRecorder(store, settings.llm_model)
        self.agent_runtime = ReActRuntime(
            self.planner.llm,
            self.tools,
            ToolPolicyGuard(),
            settings=settings,
            recorder=self.agent_runs,
        )

    def set_dispatcher(self, dispatcher: ApplicationDispatcher) -> None:
        self.dispatcher = dispatcher

    async def enqueue(self, message: IncomingMessage) -> None:
        key = message.chat_queue_key(self.oauth.settings.feishu_app_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            with trace_scope():
                try:
                    await self._handle(message)
                except Exception:
                    await self.audit.record(
                        "agent.run",
                        "failure",
                        tenant_key=message.tenant_key,
                        app_id=message.app_id,
                        principal_id=message.user_id,
                        chat_id=message.chat_id,
                        thread_id=message.thread_id or message.root_id,
                        message_id=message.message_id,
                    )
                    self.logger.exception("Agent 工作流执行失败")
                    await self.transport.send_markdown(
                        message.chat_id,
                        "处理失败，详细原因已写入服务日志。请稍后重试。",
                    )
        if not lock.locked():
            self._locks.pop(key, None)

    async def _handle(self, message: IncomingMessage) -> None:
        if not await self.store.claim_event(
            message.event_id or message.message_id,
            "feishu.message",
            tenant_key=message.tenant_key,
            app_id=message.app_id,
        ):
            return
        text = clean_incoming_text(message.text)
        self.logger.info(
            "📩 收到用户消息 message_id=%s chat=%s user=%s "
            "chat_type=%s message_type=%s text_chars=%d attachments=%d content=%r",
            message.message_id,
            message.chat_id,
            message.user_id,
            message.chat_type,
            message.message_type,
            len(text),
            len(message.attachments),
            text,
        )
        attachment_context: dict[str, Any] = {}
        attachment_response: str | None = None
        # Q8：命令层优先于附件解析（注入后生效）。
        command_router = getattr(self, "command_router", None)
        if command_router is not None and text:
            command_result = await command_router.try_handle(
                message,
                text,
                date=self.planner.today(),
            )
            if command_result is not None:
                await self.transport.send_markdown(message.chat_id, command_result.message)
                return
        if message.attachments:
            if self.attachment_service is None:
                await self.transport.send_markdown(message.chat_id, "附件功能当前不可用。")
                return
            attachment_kinds = {item.kind for item in message.attachments}
            status = (
                "⏳ 已收到图片，正在读取和分析…"
                if attachment_kinds == {"image"}
                else "⏳ 已收到附件，正在读取和处理…"
            )
            await self.transport.send_markdown(message.chat_id, status)
            self.logger.info(
                "📎 开始处理附件 message_id=%s kinds=%s count=%d",
                message.message_id,
                ",".join(sorted(attachment_kinds)),
                len(message.attachments),
            )
            try:
                prepared = await self.attachment_service.prepare(message, text)
            except FeishuMediaPermissionError:
                self.logger.warning(
                    "附件预处理失败：飞书应用缺少 im:message:readonly 权限 "
                    "message_id=%s",
                    message.message_id,
                )
                await self.transport.send_markdown(
                    message.chat_id,
                    "图片/语音/文件读取权限未开通。请管理员在飞书开放平台为应用开通 "
                    "**im:message:readonly**，发布新版本后重试。",
                )
                return
            except VisionTimeoutError:
                self.logger.warning(
                    "附件图片理解超时 message_id=%s timeout=%ss retries=%d",
                    message.message_id,
                    self.oauth.settings.song_agent_vision_read_timeout_seconds,
                    self.oauth.settings.song_agent_vision_max_retries,
                )
                await self.transport.send_markdown(
                    message.chat_id,
                    "图片已经收到，但图片理解服务响应超时；本次没有自动重试。"
                    "请稍后重新发送。",
                )
                return
            except (VisionBusyError, VisionConnectionError) as error:
                self.logger.warning(
                    "附件图片理解服务不可用 message_id=%s error_type=%s retries=%d",
                    message.message_id,
                    type(error).__name__,
                    self.oauth.settings.song_agent_vision_max_retries,
                )
                await self.transport.send_markdown(
                    message.chat_id,
                    "图片已经收到，但图片理解服务当前繁忙；本次没有自动重试。"
                    "请稍后重新发送。",
                )
                return
            except Exception:
                self.logger.exception(
                    "附件预处理失败 message_id=%s",
                    message.message_id,
                )
                await self.transport.send_markdown(
                    message.chat_id,
                    "附件处理失败，请检查格式、大小或稍后重试。",
                )
                return
            text = prepared.text
            attachment_context = prepared.context
            attachment_response = prepared.direct_response
            self.logger.info(
                "✅ 附件处理完成 message_id=%s text_chars=%d direct_response=%s",
                message.message_id,
                len(text),
                bool(attachment_response),
            )
        elif message.message_type not in {"text", "post"}:
            await self.transport.send_markdown(message.chat_id, "当前版本只接收文字消息。")
            return
        if not text:
            return
        if attachment_response:
            request = UserRequest(
                identity=message.identity(self.oauth.settings.feishu_app_id),
                text=text,
                source="feishu",
                chat_id=message.chat_id,
                thread_id=message.thread_id or message.root_id,
                message_id=message.message_id,
                event_id=message.event_id,
                context=attachment_context,
            )
            conversation_contexts = getattr(
                self.dispatcher,
                "conversation_contexts",
                None,
            )
            if conversation_contexts is not None:
                await conversation_contexts.record_user(request)
                await conversation_contexts.record_assistant(
                    request,
                    attachment_response,
                )
            await self.transport.send_markdown(message.chat_id, attachment_response)
            return

        if self.dispatcher is None:
            raise RuntimeError("ApplicationDispatcher 尚未配置")
        result = await self.dispatcher.dispatch(
            UserRequest(
                identity=message.identity(self.oauth.settings.feishu_app_id),
                text=text,
                source="feishu",
                chat_id=message.chat_id,
                thread_id=message.thread_id or message.root_id,
                message_id=message.message_id,
                event_id=message.event_id,
                context=attachment_context,
            )
        )
        if getattr(self, "presenter", None) is not None:
            await self.presenter.present(
                message.chat_id,
                message.identity(self.oauth.settings.feishu_app_id),
                result,
            )
        elif result.message:
            await self.transport.send_markdown(message.chat_id, render_message(result))

    async def handle_general_request(self, request: UserRequest) -> ApplicationResult:
        message = IncomingMessage(
            message_id=request.message_id,
            event_id=request.event_id,
            tenant_key=request.identity.tenant_key,
            app_id=request.identity.app_id,
            user_id=request.identity.subject_id,
            open_id=request.identity.open_id,
            tenant_user_id=request.identity.user_id,
            union_id=request.identity.union_id,
            chat_id=request.chat_id,
            thread_id=request.thread_id,
            root_id="",
            chat_type="p2p",
            message_type="text",
            text=request.text,
        )
        date = self.planner.today()
        record = await self.store.get_record(
            message.chat_id,
            message.user_id,
            date,
            tenant_key=message.tenant_key,
            app_id=message.app_id,
            thread_id=message.thread_id,
        )
        quick_response = self._try_quick_response(request.text, record, date)
        if quick_response:
            return ApplicationResult(status="ok", message=quick_response)
        context = AgentContext(
            message=message,
            user_text=request.text,
            conversation_key=message.conversation_key(
                self.oauth.settings.feishu_app_id
            ).serialize(),
            metadata={
                **request.context,
                "record": record,
                "date": date,
                "state_summary": (
                    f"今天已有{len(record.tasks)}项计划，状态={record.plan_status}"
                    if record
                    else "今天暂无计划"
                ),
                "current_time": datetime.now(
                    ZoneInfo(self.oauth.settings.timezone)
                ).isoformat(timespec="minutes"),
                "today_tasks": (
                    [
                        {"id": task.id, "title": task.title, "status": task.status}
                        for task in record.tasks
                    ]
                    if record
                    else []
                ),
            },
        )
        run_id = await self.agent_runs.start(context)
        try:
            result = await self.agent_runtime.run(context)
        except Exception:
            failed = AgentResult(
                status="failed",
                response="",
                step_count=0,
                tool_call_count=0,
                error_code="agent_runtime_error",
            )
            await self.agent_runs.finish(run_id, failed)
            raise
        await self.agent_runs.finish(run_id, result)
        await self.audit.record(
            "agent.run",
            result.status,
            tenant_key=message.tenant_key,
            app_id=message.app_id,
            principal_id=message.user_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            agent_run_id=run_id,
            decision=result.error_code or result.status,
            metadata={
                "step_count": result.step_count,
                "tool_call_count": result.tool_call_count,
                "model": self.oauth.settings.llm_model,
            },
        )
        return self._to_application_result(result)

    def _to_application_result(self, result: AgentResult) -> ApplicationResult:
        """AgentResult → ApplicationResult（Q12）：透传待确认 Action。"""
        if result.pending_action_ids:
            action_ids = list(result.pending_action_ids)
            return ApplicationResult(
                status="awaiting_confirmation",
                intent="conversation.general",
                message=result.response or "处理完成。",
                action_id=action_ids[0],
                data={"action_ids": action_ids},
            )
        return ApplicationResult(
            status="ok" if result.status == "completed" else "error",
            intent="conversation.general",
            message=result.response or "处理完成。",
        )

    def _build_tool_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        definitions = (
            (
                "plans.save_draft",
                "保存今日计划草稿。生成 ABC 分级任务；时间必须为 HH:MM。"
                "带时间任务只准备确认卡片，用户确认后才写入日历，默认提前 10 分钟提醒。",
                self._tool_plan_draft,
                "local",
                _plan_arguments_schema(),
            ),
            (
                "reviews.save",
                "根据用户反馈更新今天的本地复盘。",
                self._tool_review,
                "local",
                _review_arguments_schema(),
            ),
            (
                "documents.prepare_create",
                "准备创建飞书云文档并发送确认卡片；不会直接写文档。",
                self._tool_document_create,
                "prepare",
                _document_arguments_schema(create=True),
            ),
            (
                "documents.prepare_append",
                "查找目标文档并准备追加草稿，发送确认卡片；不会直接写文档。",
                self._tool_document_append,
                "prepare",
                _document_arguments_schema(create=False),
            ),
            (
                "websearch.search",
                "使用搜索引擎搜索信息。用于用户说「搜索」「查找」「查询」等需要联网搜索的请求。支持自动选择最佳搜索引擎。",
                self._tool_websearch,
                "local",
                _websearch_arguments_schema(),
            ),
            (
                "tool_results.read",
                "按 result_ref 读取本用户此前工具调用的原始结果。",
                self._tool_result_read,
                "local",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["result_ref"],
                    "properties": {
                        "result_ref": {
                            "type": "string",
                            "pattern": "^tool_result_[a-f0-9]+$",
                        }
                    },
                },
            ),
        )
        for name, description, handler, category, arguments_schema in definitions:
            registry.register(
                AgentTool(
                    name=name,
                    description=description,
                    handler=handler,
                    arguments_schema=arguments_schema,
                    category=category,
                )
            )
        attachment_tools = getattr(self, "attachment_tools", None)
        if attachment_tools is not None:
            attachment_definitions = (
                (
                    "attachments.analyze_image",
                    "分析当前用户已上传的图片附件；只接受 attachment_id 和分析指令。",
                    self._tool_analyze_image,
                    AnalyzeImageInput.model_json_schema(),
                ),
                (
                    "attachments.transcribe_audio",
                    "转写当前用户已上传的语音附件；只接受 attachment_id 和语言。",
                    self._tool_transcribe_audio,
                    TranscribeAudioInput.model_json_schema(),
                ),
                (
                    "attachments.parse_document",
                    "解析当前用户已上传的文档附件；只接受 attachment_id 和解析指令。",
                    self._tool_parse_document,
                    ParseDocumentInput.model_json_schema(),
                ),
            )
            for name, description, handler, arguments_schema in attachment_definitions:
                registry.register(
                    AgentTool(
                        name=name,
                        description=description,
                        handler=handler,
                        arguments_schema=arguments_schema,
                        category="local",
                    )
                )
        return registry

    def _attachment_access(self, context: AgentContext) -> AttachmentAccess:
        identity = context.message.identity(self.oauth.settings.feishu_app_id)
        return AttachmentAccess(
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            principal_id=identity.subject_id,
        )

    async def _tool_analyze_image(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        assert self.attachment_tools is not None
        result = await self.attachment_tools.analyze_image(
            self._attachment_access(context),
            AnalyzeImageInput.model_validate(arguments),
        )
        return await self.attachment_tools.as_tool_result(result, summary="图片已解析")

    async def _tool_transcribe_audio(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        assert self.attachment_tools is not None
        result = await self.attachment_tools.transcribe_audio(
            self._attachment_access(context),
            TranscribeAudioInput.model_validate(arguments),
        )
        return await self.attachment_tools.as_tool_result(result, summary="语音已转写")

    async def _tool_parse_document(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        assert self.attachment_tools is not None
        result = await self.attachment_tools.parse_document(
            self._attachment_access(context),
            ParseDocumentInput.model_validate(arguments),
        )
        return await self.attachment_tools.as_tool_result(result, summary="文档已解析")

    def _try_quick_response(self, text: str, record: DailyRecord | None, date: str) -> str | None:
        """
        快速路径：简单请求直接返回，不走完整ReAct流程。

        Args:
            text: 用户输入文本
            record: 当天的计划记录
            date: 日期字符串

        Returns:
            快速响应文本，如果需要走完整流程则返回None
        """
        normalized = text.strip().lower()

        # 问候和简单对话
        if re.fullmatch(
            r"(你好|您好|嗨|哈喽|hello|hi|在吗|谢谢|感谢|你是谁|"
            r"你能做什么|你会做什么|有什么功能|能帮我做什么)[？?！!。.～~\s]*",
            normalized,
        ):
            return (
                "你好！我是宋管家，你的飞书个人助手。\n\n"
                "我可以帮你：\n"
                "- 📅 管理日程和提醒\n"
                "- 📝 创建和编辑文档\n"
                "- 📋 制定和复盘每日计划\n\n"
                "直接告诉我要做什么即可！"
            )

        # 简单的状态查询
        if re.match(r"^(今天|今日)?(有什么|有啥).{0,8}(事|任务|计划|安排)", normalized):
            if record and record.tasks:
                return format_plan(record)
            else:
                return f"你在今天（{date}）还没有计划。"

        # 简单的感谢回复
        if re.fullmatch(r"(谢谢|感谢|多谢|辛苦了)[！!。.～~\s]*", normalized):
            return "不客气！有什么需要随时叫我。"

        # 简单的告别
        if re.fullmatch(r"(再见|拜拜|晚安|goodbye|bye)[！!。.～~\s]*", normalized):
            return "再见！祝你今天愉快。"

        # 需要走完整流程的复杂请求
        return None

    async def _tool_plan_draft(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        parsed = PlanOutput.model_validate(arguments)
        await self._create_draft(
            context.message,
            context.metadata.get("record"),
            context.user_text,
            str(context.metadata["date"]),
            parsed,
        )
        return ToolResult(
            status="ok",
            summary="计划草稿已处理",
            terminal=True,
            response="计划草稿已生成。请查看计划；带时间事项可在确认卡片中选择是否写入日历。",
        )

    async def _tool_review(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        parsed = ReviewOutput.model_validate(arguments)
        await self._create_review(
            context.message,
            context.metadata.get("record"),
            parsed,
        )
        return ToolResult(status="ok", summary="复盘已处理", terminal=True)

    async def _tool_document_create(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        draft = DocumentOutput.model_validate({"action": "create", **arguments})
        action = await self._create_document(context.message, context.user_text, draft)
        if action is None:
            return ToolResult(status="ok", summary="文档草稿未生成", terminal=True)
        return ToolResult(
            status="ok",
            summary="文档草稿已处理",
            terminal=True,
            response="文档草稿已生成，请在确认卡片中核对后执行。",
            pending_action_ids=(action.action_id,),
        )

    async def _tool_document_append(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        draft = DocumentOutput.model_validate({"action": "append", **arguments})
        action = await self._create_document(context.message, context.user_text, draft)
        if action is None:
            return ToolResult(status="ok", summary="文档草稿未生成", terminal=True)
        return ToolResult(
            status="ok",
            summary="文档草稿已处理",
            terminal=True,
            response="文档草稿已生成，请在确认卡片中核对后执行。",
            pending_action_ids=(action.action_id,),
        )

    async def _tool_websearch(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        """执行网络搜索"""
        query = arguments.get("query", "")
        provider = arguments.get("provider", "auto")
        max_results = arguments.get("max_results", 5)
        search_depth = arguments.get("search_depth", "basic")
        include_answer = arguments.get("include_answer", False)

        if not query:
            return ToolResult(
                status="error",
                summary="搜索查询不能为空",
                terminal=True,
            )

        try:
            results = await self.search_mcp.search(
                query,
                provider=provider,
                max_results=max_results,
                search_depth=search_depth,
                include_answer=include_answer,
            )

            if not results:
                return ToolResult(
                    status="ok",
                    summary="未找到相关结果",
                )

            raw_result = {
                "query": query,
                "provider": provider,
                "items": [
                    {
                        "title": result.title,
                        "snippet": result.snippet,
                        "url": result.url,
                        "source": result.source,
                    }
                    for result in results
                ],
            }
            result_ref = await self.store.save_tool_result(
                tenant_key=context.message.tenant_key,
                app_id=context.message.app_id,
                principal_id=context.message.user_id,
                tool_name="websearch.search",
                summary=f"找到 {len(results)} 条与“{query}”相关的结果",
                payload=raw_result,
                truncated=len(results) > max_results,
            )
            _store_retrieved_context(
                context.metadata,
                result_ref,
                {
                    "summary": f"找到 {len(results)} 条与“{query}”相关的结果",
                    "items": raw_result["items"][:3],
                    "truncated": len(results) > 3,
                },
            )

            formatted_results = []
            for i, result in enumerate(results, 1):
                formatted_results.append(
                    f"{i}. **{result.title}**（{result.source}）\n"
                    f"   {result.snippet}\n"
                    f"   [链接]({result.url})"
                )

            # 构建响应并检查长度限制
            header = f"搜索结果（{provider}，引用 {result_ref}）：\n\n"
            max_length = 1900  # 留一些余量

            response_parts = []
            current_length = len(header)

            for result_text in formatted_results:
                if current_length + len(result_text) + 2 > max_length:
                    # 达到长度限制，截断并添加提示
                    remaining = len(results) - len(response_parts)
                    response_parts.append(f"\n\n...（还有 {remaining} 条结果未显示）")
                    break
                response_parts.append(result_text)
                current_length += len(result_text) + 2

            response = header + "\n\n".join(response_parts)
            return ToolResult(
                status="ok",
                summary=response,
            )

        except Exception as e:
            self.logger.exception("网络搜索工具失败")
            return ToolResult(
                status="error",
                summary=f"搜索失败：{e}",
            )

    async def _tool_result_read(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        result_ref = str(arguments["result_ref"])
        result = await self.store.get_tool_result(
            result_ref,
            tenant_key=context.message.tenant_key,
            app_id=context.message.app_id,
            principal_id=context.message.user_id,
        )
        if not result:
            return ToolResult(status="error", summary="工具结果不存在、已过期或无权访问")
        summary = _format_stored_tool_result(result)
        _store_retrieved_context(context.metadata, result_ref, summary)
        return ToolResult(
            status="ok",
            summary=summary,
        )

    async def _create_draft(
        self,
        message: IncomingMessage,
        existing: DailyRecord | None,
        text: str,
        date: str,
        parsed: PlanOutput,
    ) -> None:
        has_time_hint = has_explicit_time_hint(text)
        self.logger.info(
            "🧠 计划处理：解析任务 has_existing=%s has_time_hint=%s",
            bool(existing),
            has_time_hint,
        )

        if existing and existing.plan_status == "confirmed" and not has_time_hint:
            await self.transport.send_markdown(
                message.chat_id,
                "你今天的计划已经确认。若需重做，请先发送 `/clear` 或 `清空`，"
                "并自行检查你日历中的旧日程。\n\n"
                '如果是想添加**带时间的提醒**，请说"X分钟后提醒我..."或"X点提醒我..."。',
            )
            return

        tasks = parsed.tasks
        if not tasks:
            await self.transport.send_markdown(
                message.chat_id,
                "可以，请把今天要做的具体事项告诉我，例如：吃饭、购物、提交材料。有明确时间也可以一起说。",
            )
            return
        now = datetime.now(UTC).isoformat()

        new_task_ids: set[str]
        if existing and has_time_hint:
            # 追加新的带时间的任务到现有计划
            existing_tasks = list(existing.tasks)
            new_tasks = build_tasks(tasks)
            # 重新编号新任务
            counters = {"A": 0, "B": 0, "C": 0}
            for t in existing_tasks:
                counters[t.priority] = counters.get(t.priority, 0) + 1
            for t in new_tasks:
                counters[t.priority] = counters.get(t.priority, 0) + 1
                t.id = f"{t.priority}{counters[t.priority]}"
            new_task_ids = {task.id for task in new_tasks}
            all_tasks = existing_tasks + new_tasks
            record = DailyRecord(
                key=self.store.record_key(
                    message.chat_id,
                    message.user_id,
                    date,
                    tenant_key=message.tenant_key,
                    app_id=message.app_id,
                    thread_id=message.thread_id or message.root_id,
                ),
                date=date,
                tenant_key=message.tenant_key,
                app_id=message.app_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id or message.root_id,
                user_id=message.user_id,
                # 新增的带时间事项必须重新确认，不能沿用旧计划的 confirmed 状态。
                plan_status="draft",
                tasks=all_tasks,
                created_at=existing.created_at,
                updated_at=now,
            )
        else:
            record = DailyRecord(
                key=self.store.record_key(
                    message.chat_id,
                    message.user_id,
                    date,
                    tenant_key=message.tenant_key,
                    app_id=message.app_id,
                    thread_id=message.thread_id or message.root_id,
                ),
                date=date,
                tenant_key=message.tenant_key,
                app_id=message.app_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id or message.root_id,
                user_id=message.user_id,
                plan_status="draft",
                tasks=build_tasks(tasks),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            new_task_ids = {task.id for task in record.tasks}
        await self.store.save_record(record)
        # 旧版“计划写日历”确认卡片路径已随 Q6 移除（record_key action 不再
        # 创建，存量数据由 0014 迁移退休）。带时间任务现在只保存在本地计划，
        # 用户如需写入日历请使用“创建日程”意图。
        self.logger.info(
            "🧠 计划处理：保存计划完成 new_task_ids=%s all_tasks=%s",
            new_task_ids,
            [(t.id, t.title, t.start_time) for t in record.tasks],
        )
        await self.transport.send_markdown(message.chat_id, format_plan(record))

    async def _create_document(
        self,
        message: IncomingMessage,
        text: str,
        draft: DocumentOutput,
    ) -> PendingAction | None:
        """准备文档草稿并创建 PendingAction；None 表示已发提示（授权/错误）。"""
        self.logger.info("🧠 文档处理：使用 ReAct 已校验草稿 action=%s", draft.action)
        explicit_token = extract_docx_token(text)
        required_scopes = (
            ("docx:document", "search:docs:read")
            if draft.action == "append" and not explicit_token
            else ("docx:document",)
        )
        identity = message.identity(self.oauth.settings.feishu_app_id)
        token_context = await self.oauth.get_valid_token_context(identity, required_scopes)
        if not token_context:
            url = await self.oauth.create_authorization_url(
                identity,
                message.chat_id,
                required_scopes,
                original_request=text,
            )
            await self.transport.send_markdown(
                message.chat_id,
                f"创建云文档需要你本人授权。[点击这里完成授权]({url})，授权后会自动继续处理。"
                "新建文档会进入你自己的云空间；追加操作会写入你有权限的目标文档。",
            )
            return None
        if draft.action == "append":
            target_title = (draft.target_title or draft.title or "").strip()
            generic_target = target_title in {
                "",
                "我的文档",
                "文档",
                "该文档",
                "这个文档",
                "刚才的文档",
                "上一份文档",
            }
            recent = await self.store.get_document_binding(
                message.chat_id,
                message.user_id,
                tenant_key=message.tenant_key,
                app_id=message.app_id,
                thread_id=message.thread_id or message.root_id,
            )
            if generic_target and recent and not explicit_token:
                target_title = recent.title
                explicit_token = recent.token
            if not target_title and not explicit_token:
                await self.transport.send_markdown(
                    message.chat_id, "请告诉我要追加的文档标题，或直接附上目标飞书文档链接。"
                )
                return None
            if re.search(r"这个|这项|该任务|上述|上面|刚才", text) and "待补充" in draft.markdown:
                await self.transport.send_markdown(
                    message.chat_id,
                    "我识别到你要追加文档，但“这个任务”的具体内容不在当前消息中。"
                    "请把任务内容写在同一条指令里；我不会把“待补充”或猜测内容写进群文档。",
                )
                return None
            target_token = explicit_token
            target_url = (
                recent.url
                if recent and target_token == recent.token
                else f"https://feishu.cn/docx/{target_token}"
                if target_token
                else ""
            )
            if not target_token:
                await self.transport.send_markdown(
                    message.chat_id, f"正在该群关联的云文档中查找“{target_title}”..."
                )
                documents = await self.openapi.search_documents(
                    target_title,
                    token_context,
                )
                exact = [item for item in documents if item.title.strip() == target_title]
                candidates = exact or documents
                if not candidates:
                    await self.transport.send_markdown(
                        message.chat_id,
                        f"没有在该群中找到“{target_title}”。请在消息中附上目标飞书文档链接后重试。",
                    )
                    return None
                if len(candidates) > 1:
                    links = "\n".join(f"- [{item.title}]({item.url})" for item in candidates[:10])
                    await self.transport.send_markdown(
                        message.chat_id,
                        f"找到多个可能的目标文档，请把正确文档链接附在指令中：\n{links}",
                    )
                    return None
                target_token = candidates[0].token
                target_url = candidates[0].url
            action = await self.pending_actions.create_document_action(
                message,
                action_type="document.append",
                title=target_title,
                markdown=draft.markdown,
                document_token=target_token,
                document_url=target_url,
            )
            return action
        else:
            title = draft.title or "Agent 云文档"
            action = await self.pending_actions.create_document_action(
                message,
                action_type="document.create",
                title=title,
                markdown=draft.markdown,
            )
            return action

    async def _create_review(
        self,
        message: IncomingMessage,
        record: DailyRecord | None,
        parsed: ReviewOutput,
    ) -> None:
        self.logger.info("🧠 复盘处理：使用 ReAct 已校验结果")
        if not record:
            await self.transport.send_markdown(message.chat_id, "找不到你今天的计划，暂时无法复盘。")
            return
        updates = {item.task_id: item for item in parsed.updates}
        parsed.updates = [
            updates.get(task.id, ReviewUpdate(task_id=task.id, status="unconfirmed")) for task in record.tasks
        ]
        rate = apply_review(record, parsed.updates)
        record.review = DailyReview(
            created_at=datetime.now(UTC).isoformat(),
            completion_rate=rate,
            summary=parsed.summary,
            insights=parsed.insights,
        )
        record.updated_at = datetime.now(UTC).isoformat()
        await self.store.save_record(record)
        await self.transport.send_markdown(message.chat_id, format_review(record))

    def _log_background_error(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except Exception:
            self.logger.exception("后台状态消息发送失败")


def _format_stored_tool_result(
    result: dict[str, Any],
    *,
    max_length: int = 1900,
) -> str:
    """按完整来源条目压缩工具结果，避免在 JSON 或句子中间硬截断。"""
    header = str(result.get("summary") or "工具结果")
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return header
    items = payload.get("items")
    if not isinstance(items, list):
        keys = [str(key) for key in payload]
        visible = keys[:20]
        suffix = f"；另有 {len(keys) - 20} 个字段" if len(keys) > 20 else ""
        return f"{header}\n数据字段：{'、'.join(visible)}{suffix}"

    parts = [header]
    included = 0
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        title = _shorten_at_boundary(str(item.get("title") or "未命名"), 120)
        snippet = _shorten_at_boundary(str(item.get("snippet") or ""), 360)
        url = str(item.get("url") or "")
        source = str(item.get("source") or "未知来源")
        block = f"{index}. [{source}] {title}\n{snippet}\n{url}".strip()
        candidate = "\n\n".join([*parts, block])
        if len(candidate) > max_length:
            break
        parts.append(block)
        included += 1
    omitted = len(items) - included
    if omitted:
        notice = f"其余 {omitted} 条未展开；可按引用继续读取。"
        candidate = "\n\n".join([*parts, notice])
        if len(candidate) <= max_length:
            parts.append(notice)
    return "\n\n".join(parts)


def _store_retrieved_context(
    metadata: dict[str, Any],
    result_ref: str,
    value: Any,
) -> None:
    """兼容附件列表与工具结果字典两种检索上下文。"""

    retrieved = metadata.get("retrieved_context")
    if isinstance(retrieved, list):
        item = {"result_ref": result_ref}
        if isinstance(value, dict):
            item.update(value)
        else:
            item["content"] = value
        retrieved.append(item)
        return
    if not isinstance(retrieved, dict):
        retrieved = {}
        metadata["retrieved_context"] = retrieved
    retrieved[result_ref] = value


def _shorten_at_boundary(text: str, max_length: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= max_length:
        return normalized
    window = normalized[:max_length]
    boundary = max(window.rfind(mark) for mark in ("。", "！", "？", ".", "!", "?"))
    if boundary >= max_length // 2:
        return window[: boundary + 1]
    return window.rstrip("，,；;：: ") + "…"


def _plan_arguments_schema() -> dict[str, Any]:
    nullable_time = {
        "anyOf": [
            {"type": "string", "pattern": r"^([01]\d|2[0-3]):[0-5]\d$"},
            {"type": "null"},
        ]
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["tasks"],
        "properties": {
            "tasks": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "title",
                        "priority",
                        "start_time",
                        "end_time",
                        "repeat",
                    ],
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "priority": {"type": "string", "enum": ["A", "B", "C"]},
                        "start_time": nullable_time,
                        "end_time": nullable_time,
                        "repeat": {
                            "type": "string",
                            "enum": ["none", "daily", "weekdays", "weekly"],
                        },
                    },
                    "description": (
                        "任务项。名称字段必须是 title，不是 name。start_time 和 "
                        "end_time 必须是 HH:MM 格式（如 17:26），不要使用 ISO。"
                        "用户给出开始时间但未说时长时，补合理 end_time。"
                    ),
                },
            }
        },
    }


def _review_arguments_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["updates", "summary", "insights"],
        "properties": {
            "updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_id", "status", "completion_ratio"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["completed", "partial", "not_done", "unconfirmed"],
                        },
                        "completion_ratio": {
                            "anyOf": [
                                {"type": "number", "minimum": 0, "maximum": 100},
                                {"type": "null"},
                            ]
                        },
                    },
                },
            },
            "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
            "insights": {
                "type": "array",
                "maxItems": 5,
                "items": {"type": "string"},
            },
        },
    }


def _document_arguments_schema(*, create: bool) -> dict[str, Any]:
    nullable_title = {
        "anyOf": [
            {"type": "string", "minLength": 1, "maxLength": 100},
            {"type": "null"},
        ]
    }
    properties: dict[str, Any] = {
        "title": nullable_title,
        "target_title": nullable_title,
        "markdown": {"type": "string", "minLength": 1, "maxLength": 100_000},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "target_title", "markdown"],
        "properties": properties,
        "description": (
            "新建文档：title 必填，target_title=null。"
            if create
            else "追加文档：target_title 填用户给出的目标标题；若消息含文档链接可为 null。"
        ),
    }


def _websearch_arguments_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "搜索查询字符串",
            },
            "provider": {
                "type": "string",
                "enum": ["auto", "searxng", "talordata", "you", "tavily"],
                "default": "auto",
                "description": "搜索引擎提供者（auto 自动选择最佳提供者）",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
                "description": "最大返回结果数",
            },
            "search_depth": {
                "type": "string",
                "enum": ["basic", "advanced"],
                "default": "basic",
                "description": "搜索深度（仅 Tavily 支持）",
            },
            "include_answer": {
                "type": "boolean",
                "default": False,
                "description": "是否包含 AI 生成的答案（仅 Tavily 和 TalorData 支持）",
            },
        },
    }


def extract_docx_token(text: str) -> str | None:
    match = re.search(r"https?://[^\s)]+/docx/([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else None


