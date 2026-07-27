"""
Agent 工作流模块。

处理消息分发、意图识别、计划创建、文档生成等核心业务逻辑。
支持自然语言交互，自动识别用户意图。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .agent.context import AgentContext
from .agent.models import AgentResult, ToolResult
from .agent.runtime import AgentLimits, ReActRuntime
from .agent.tool_registry import AgentTool, ToolRegistry
from .application.request_router import RequestRouter
from .application.result_renderer import render_message
from .domain.intents import UserRequest
from .domain.results import ApplicationResult
from .feishu.cards import action_confirmation_markdown
from .feishu.mcp import FeishuMcp
from .feishu.oauth import FeishuOAuth
from .feishu.openapi import FeishuOpenApi
from .feishu.transport import FeishuTransport, clean_incoming_text
from .models import (
    DailyRecord,
    DailyReview,
    DocumentBinding,
    DocumentOutput,
    FeishuIdentity,
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
    is_clear_command,
    is_confirmation,
    is_reminder_status_question,
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
        mcp: FeishuMcp,
        search_mcp: SearchMcp,
        pending_actions: PendingActionService,
        audit: AuditService,
    ) -> None:
        self.planner = planner
        self.store = store
        self.transport = transport
        self.oauth = oauth
        self.openapi = openapi
        self.mcp = mcp
        self.search_mcp = search_mcp
        self.pending_actions = pending_actions
        self.audit = audit
        self.logger = logging.getLogger(__name__)
        self._locks: dict[str, asyncio.Lock] = {}
        self.notify_outbox: Callable[[], None] = lambda: None
        self.request_router: RequestRouter | None = None
        self.tools = self._build_tool_registry()
        settings = self.oauth.settings
        self.agent_runs = AgentRunRecorder(store, settings.llm_model)
        self.agent_runtime = ReActRuntime(
            self.planner.llm,
            self.tools,
            ToolPolicyGuard(),
            AgentLimits(
                max_steps=settings.agent_max_steps,
                max_tool_calls=settings.agent_max_tool_calls,
                max_consecutive_tool_errors=settings.agent_max_consecutive_errors,
                timeout_seconds=settings.agent_run_timeout_seconds,
            ),
            recorder=self.agent_runs,
            settings=settings,
        )

    def set_request_router(self, router: RequestRouter) -> None:
        self.request_router = router

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
        if message.message_type not in {"text", "post"}:
            await self.transport.send_markdown(message.chat_id, "当前版本只接收文字消息。")
            return
        text = clean_incoming_text(message.text)
        if not text:
            return
        self.logger.info(
            "📩 收到用户消息 message_id=%s chat=%s user=%s type=%s "
            "text_chars=%d content=%r",
            message.message_id,
            message.chat_id,
            message.user_id,
            message.chat_type,
            len(text),
            text,
        )
        date = self.planner.today()
        record = await self.store.get_record(
            message.chat_id,
            message.user_id,
            date,
            tenant_key=message.tenant_key,
            app_id=message.app_id,
            thread_id=message.thread_id or message.root_id,
        )

        if re.match(r"^/(help|帮助)\b", text, re.IGNORECASE):
            await self.transport.send_markdown(message.chat_id, help_message())
        elif re.match(r"^/(status|状态)\b", text, re.IGNORECASE):
            await self.transport.send_markdown(
                message.chat_id, format_plan(record) if record else f"你在今天（{date}）还没有计划。"
            )
        elif is_clear_command(text):
            await self.store.delete_record(
                message.chat_id,
                message.user_id,
                date,
                tenant_key=message.tenant_key,
                app_id=message.app_id,
                thread_id=message.thread_id or message.root_id,
            )
            await self.transport.send_markdown(
                message.chat_id, "已清空你今天的本地计划；不会影响其他群成员。"
            )
        elif is_reminder_status_question(text):
            await self._reminder_status(message, record)
        elif is_confirmation(text):
            await self.transport.send_markdown(
                message.chat_id,
                "为防止串用户或确认错草稿，文字“确认”已停用。请点击最新计划卡片上的 **确认创建**。",
            )
        else:
            if self.request_router is None:
                raise RuntimeError("RequestRouter 尚未配置")
            await self.transport.send_markdown(
                message.chat_id,
                "⏳ 正在处理你的请求，请稍候…",
            )
            result = await self.request_router.handle(
                UserRequest(
                    identity=message.identity(self.oauth.settings.feishu_app_id),
                    text=text,
                    source="feishu",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id or message.root_id,
                    message_id=message.message_id,
                    event_id=message.event_id,
                )
            )
            if result.status == "awaiting_confirmation":
                action_ids = result.data.get("action_ids")
                if not isinstance(action_ids, list):
                    action_ids = [result.action_id]
                for action_id in action_ids:
                    action = await self.store.get_pending_action(str(action_id))
                    if not action:
                        raise RuntimeError("应用服务返回的 PendingAction 不存在")
                    await self.transport.send_confirmation_card(
                        message.chat_id,
                        action_confirmation_markdown(action),
                        action,
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
        return registry

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
        await self._create_document(context.message, context.user_text, draft)
        return ToolResult(status="ok", summary="文档草稿已处理", terminal=True)

    async def _tool_document_append(
        self,
        context: AgentContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        draft = DocumentOutput.model_validate({"action": "append", **arguments})
        await self._create_document(context.message, context.user_text, draft)
        return ToolResult(status="ok", summary="文档草稿已处理", terminal=True)

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
            context.metadata.setdefault("retrieved_context", {})[result_ref] = {
                "summary": f"找到 {len(results)} 条与“{query}”相关的结果",
                "items": raw_result["items"][:3],
                "truncated": len(results) > 3,
            }

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
        context.metadata.setdefault("retrieved_context", {})[result_ref] = summary
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
        timed_task_ids = {
            task.id
            for task in record.tasks
            if task.id in new_task_ids and task.start_time and not task.calendar_event_id
        }
        self.logger.info(
            "🧠 计划处理：检查是否需要确认卡片 new_task_ids=%s timed_task_ids=%s "
            "tasks_with_time=%s encrypt_key_configured=%s",
            new_task_ids,
            timed_task_ids,
            [(t.id, t.title, t.start_time) for t in record.tasks if t.start_time],
            bool(self.oauth.settings.feishu_encrypt_key),
        )
        if timed_task_ids:
            if not self.oauth.settings.feishu_encrypt_key:
                await self.transport.send_markdown(
                    message.chat_id,
                    format_plan(record)
                    + "\n\n⚠️ 管理员尚未配置安全的飞书卡片回调加密"
                    "（FEISHU_ENCRYPT_KEY），当前不会执行日历写入。",
                )
                return
            action = await self.pending_actions.create_calendar_action(
                message,
                record,
                timed_task_ids,
            )
            await self.transport.send_confirmation_card(
                message.chat_id,
                format_plan(record)
                + "\n\n**是否加入飞书日历？**"
                "\n点击确认后才会写入；默认提前 **10 分钟**提醒。"
                "\n如需其他提醒时间，请先取消，再说明提前分钟数。",
                action,
            )
            self.logger.info(
                "🧠 计划处理：已发送确认卡片 action_id=%s action_type=%s",
                action.action_id,
                action.action_type,
            )
        else:
            self.logger.info(
                "🧠 计划处理：无带时间任务，直接发送文本 new_task_ids=%s all_tasks=%s",
                new_task_ids,
                [(t.id, t.title, t.start_time) for t in record.tasks],
            )
            await self.transport.send_markdown(message.chat_id, format_plan(record))

    async def _reminder_status(self, message: IncomingMessage, record: DailyRecord | None) -> None:
        timed = [task for task in record.tasks if task.start_time] if record else []
        if not timed:
            await self.transport.send_markdown(message.chat_id, "今天还没有带时间的提醒或日程。")
            return
        created = [task for task in timed if task.calendar_event_id]
        pending = [task for task in timed if not task.calendar_event_id]
        lines: list[str] = []
        if created:
            lines.append(
                "✅ 已创建到你本人日历："
                + "、".join(f"{task.title}（{task.start_time}）" for task in created)
            )
        if pending:
            lines.append(
                "⏳ 尚未创建："
                + "、".join(f"{task.title}（{task.start_time}）" for task in pending)
                + "。请点击最新计划卡片上的 **确认创建**。"
            )
        await self.transport.send_markdown(message.chat_id, "\n\n".join(lines))

    async def _create_document(
        self,
        message: IncomingMessage,
        text: str,
        draft: DocumentOutput,
    ) -> None:
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
            return
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
                return
            if re.search(r"这个|这项|该任务|上述|上面|刚才", text) and "待补充" in draft.markdown:
                await self.transport.send_markdown(
                    message.chat_id,
                    "我识别到你要追加文档，但“这个任务”的具体内容不在当前消息中。"
                    "请把任务内容写在同一条指令里；我不会把“待补充”或猜测内容写进群文档。",
                )
                return
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
                    return
                if len(candidates) > 1:
                    links = "\n".join(f"- [{item.title}]({item.url})" for item in candidates[:10])
                    await self.transport.send_markdown(
                        message.chat_id,
                        f"找到多个可能的目标文档，请把正确文档链接附在指令中：\n{links}",
                    )
                    return
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
            await self.transport.send_confirmation_card(
                message.chat_id,
                f"**目标文档：** [{target_title}]({target_url})\n\n"
                f"**追加内容预览：**\n\n{draft.markdown[:1200]}",
                action,
            )
        else:
            title = draft.title or "Agent 云文档"
            action = await self.pending_actions.create_document_action(
                message,
                action_type="document.create",
                title=title,
                markdown=draft.markdown,
            )
            await self.transport.send_confirmation_card(
                message.chat_id,
                f"**新文档标题：** {title}\n\n**内容预览：**\n\n{draft.markdown[:1200]}",
                action,
            )

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

    async def execute_pending_action(self, action: PendingAction) -> None:
        if action.action_type.startswith("document."):
            await self._execute_pending_document(action)
        elif action.action_type == "calendar.create" and isinstance(
            action.payload.get("record_key"), str
        ):
            await self._execute_pending_calendar(action)
        else:
            await self.store.mark_action_unknown(
                action.action_id,
                error_code="unsupported_action_type",
                error_message=f"不支持的动作类型: {action.action_type}",
            )

    async def _execute_pending_document(self, action: PendingAction) -> None:
        remote_call_started = False
        try:
            if not await self.store.claim_action_execution(
                action.action_id,
                worker_id=f"card-callback:{id(self)}",
            ):
                return
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
                return
            title = str(action.payload.get("title") or "Agent 云文档")
            markdown = str(action.payload.get("markdown") or "")
            if not markdown:
                await self.store.expire_pending_action(action.action_id)
                await self.transport.send_markdown(action.chat_id, "文档草稿内容为空，已拒绝执行。")
                return
            if action.action_type == "document.append":
                document_token = str(action.payload.get("document_token") or "")
                if not document_token:
                    await self.store.expire_pending_action(action.action_id)
                    await self.transport.send_markdown(action.chat_id, "目标文档标识缺失，已拒绝执行。")
                    return
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

    async def _execute_pending_calendar(self, action: PendingAction) -> None:
        remote_call_started = False
        try:
            if not await self.store.claim_action_execution(
                action.action_id,
                worker_id=f"plan-calendar:{id(self)}",
            ):
                return
            record = await self.store.get_record_by_key(
                str(action.payload["record_key"])
            )
            if (
                not record
                or record.user_id != action.creator_subject_id
                or record.updated_at != action.payload.get("record_updated_at")
            ):
                await self.store.expire_pending_action(action.action_id)
                await self.transport.send_markdown(
                    action.chat_id,
                    "该确认卡片对应的计划已变化或不存在，已拒绝执行。请重新提交计划。",
                )
                return
            identity = FeishuIdentity(
                tenant_key=action.tenant_key,
                app_id=action.app_id,
                open_id=action.creator_open_id,
                union_id=action.creator_subject_id,
            )
            scopes = ("calendar:calendar", "calendar:calendar:readonly")
            token_context = await self.oauth.get_valid_token_context(identity, scopes)
            if not token_context:
                await self.store.finish_pending_action(
                    action.action_id,
                    success=False,
                )
                url = await self.oauth.create_authorization_url(
                    identity,
                    action.chat_id,
                    scopes,
                )
                await self.transport.send_markdown(
                    action.chat_id,
                    f"创建日程需要你本人授权。[点击这里完成授权]({url})，"
                    "授权后重新提交计划。",
                )
                return
            task_ids = {str(item) for item in action.payload.get("task_ids", [])}
            remote_call_started = True
            result = await self.openapi.create_events(
                record,
                token_context,
                task_ids,
            )
            event_ids = dict(result.created)
            if event_ids:
                await self.store.record_action_remote_success(
                    action.action_id,
                    remote_resource_id=json.dumps(event_ids, sort_keys=True),
                )
            for plan_task in record.tasks:
                if plan_task.id in event_ids:
                    plan_task.calendar_event_id = event_ids[plan_task.id]
            record.plan_status = "draft" if result.failed else "confirmed"
            record.updated_at = datetime.now(UTC).isoformat()
            await self.store.save_record(record)
            await self.store.finish_pending_action(
                action.action_id,
                success=not result.failed,
            )
            await self.audit.record(
                action.action_type,
                "partial" if result.failed else "success",
                tenant_key=action.tenant_key,
                app_id=action.app_id,
                principal_id=action.creator_subject_id,
                chat_id=action.chat_id,
                thread_id=action.thread_id,
                action_id=action.action_id,
                risk_level="high",
                payload_hash=action.payload_hash,
                metadata={
                    "created_count": len(result.created),
                    "failed_count": len(result.failed),
                },
            )
            lines = [
                f"已在你本人的日历创建 {len(result.created)} 个日程，"
                "默认提前 10 分钟提醒。"
            ]
            if result.failed:
                failed_ids = "、".join(item[0] for item in result.failed)
                lines.append(f"创建失败：{failed_ids}。请重新提交失败事项。")
            await self.transport.send_markdown(action.chat_id, "\n".join(lines))
        except Exception as error:
            if remote_call_started:
                await self.store.mark_action_unknown(
                    action.action_id,
                    error_code="calendar_remote_result_uncertain",
                    error_message=str(error),
                )
            else:
                await self.store.finish_pending_action(
                    action.action_id,
                    success=False,
                )
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
            self.logger.exception(
                "执行待确认计划日历动作失败 action_id=%s",
                action.action_id,
            )
            await self.transport.send_markdown(
                action.chat_id,
                (
                    "日历请求远端结果暂时无法确认，已停止自动重试并进入核对队列。"
                    if remote_call_started
                    else "日历创建失败；计划草稿仍保留，可重新提交。"
                ),
            )


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


def help_message() -> str:
    return "\n".join(
        [
            "你好！我是**宋管家**，你的 AI 个人管理助手。以下是我能帮你做的事情：",
            "",
            "---",
            "",
            "## 📋 每日规划与复盘",
            "",
            "- **早规划**：帮你整理当天任务，按优先级分为：",
            "  - **A（必须完成）**、**B（重要）**、**C（可选）**",
            "  - 有时间要求的按时间排序，没时间的标注「灵活安排」",
            "- **晚复盘**：回顾当日完成情况，整理未完成原因、经验阻碍，并列出明日计划",
            "- 支持将规划或复盘内容**归档保存**到飞书云文档「每日记录」",
            "",
            "---",
            "",
            "## ⏰ 提醒与日程",
            "",
            "- 创建**单次提醒**或**周期提醒**（每天/工作日/每周）",
            "- 可设置提前提醒时间，到点自动通知你",
            "",
            "---",
            "",
            "## 📄 飞书云文档",
            "",
            "- 查看文档信息与正文内容",
            "- 在文档末尾**追加内容**（不覆盖已有内容）",
            "- 搜索文档",
            "",
            "---",
            "",
            "## ⚙️ 我的原则",
            "",
            "- 只依据你**明确表达**的内容来记录，不猜测、不捏造",
            "- 操作结果如实反馈，成功与否都说清楚",
            "- 涉及提醒、发消息、写文档时，必须实际调用工具，不会口头敷衍",
            "",
            "---",
            "",
            "有什么需要我帮忙的？比如做今天的规划，或者设置一个提醒？",
        ]
    )
