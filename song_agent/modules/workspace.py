from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..kernel.intent import (
    ConfirmationPolicy,
    IdempotencyPolicy,
    IntentSpec,
    OperationKind,
    RiskLevel,
    UnknownResolutionPolicy,
)
from ..kernel.models import ExecutionContext, IntentResult
from ..kernel.module import ContributionSink, ModuleManifest
from ..ports.workspace import CalendarPort, DocumentPort, ProviderCallResult, TaskPort
from ..runtime.providers import ProviderRegistry


class CalendarQueryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""
    event_id: str = ""
    start_time: str = ""
    end_time: str = ""
    page_size: int = Field(default=20, ge=1, le=100)


class CalendarCreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1000)
    start_time: str
    end_time: str
    timezone: str = "Asia/Shanghai"
    description: str = ""
    location: str = ""
    attendee_ids: list[str] = Field(default_factory=list, max_length=100)


class CalendarUpdateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1)
    fields: dict[str, Any] = Field(min_length=1)


class CalendarDeleteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1)
    calendar_id: str = ""


class TaskQueryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""
    task_id: str = ""
    completed: bool | None = None
    page_size: int = Field(default=20, ge=1, le=100)


class TaskCreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1000)
    description: str = ""
    start_time: str = ""
    due_time: str = ""
    assignee_ids: list[str] = Field(default_factory=list, max_length=100)


class TaskUpdateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1)
    fields: dict[str, Any] = Field(min_length=1)


class TaskDeleteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1)


class ReminderCreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1000)
    remind_at: str
    description: str = ""


class DocumentQueryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""
    document_id: str = ""
    page_size: int = Field(default=20, ge=1, le=100)


class DocumentCreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=1000)
    content: str = Field(default="", max_length=500_000)


class DocumentUpdateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1)
    content: str = Field(max_length=500_000)
    mode: str = Field(default="append", pattern="^(append|replace)$")


class DocumentDeleteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1)


class CalendarHandler:
    def __init__(self, providers: ProviderRegistry, operation: str) -> None:
        self.providers = providers
        self.operation = operation

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        provider: CalendarPort = self.providers.get(CalendarPort)
        values = arguments.model_dump(mode="json")
        if self.operation == "query":
            result = await provider.query(context.envelope.principal, values)
        else:
            method = getattr(provider, self.operation)
            result = await method(
                context.envelope.principal,
                values,
                idempotency_key=context.idempotency_key,
            )
        return _provider_result(f"calendar.{self.operation}d", result)

    async def reconcile(
        self, context: ExecutionContext, arguments: BaseModel
    ) -> IntentResult | None:
        provider: CalendarPort = self.providers.get(CalendarPort)
        result = await provider.reconcile(
            context.envelope.principal,
            arguments.model_dump(mode="json"),
            idempotency_key=context.idempotency_key,
        )
        return _provider_result(f"calendar.{self.operation}.reconciled", result) if result else None


class TaskHandler:
    def __init__(self, providers: ProviderRegistry, operation: str) -> None:
        self.providers = providers
        self.operation = operation

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        provider: TaskPort = self.providers.get(TaskPort)
        result = await provider.execute(
            self.operation,
            context.envelope.principal,
            arguments.model_dump(mode="json"),
            idempotency_key=context.idempotency_key,
        )
        return _provider_result(f"task.{self.operation}d", result)


class DocumentHandler:
    def __init__(self, providers: ProviderRegistry, operation: str) -> None:
        self.providers = providers
        self.operation = operation

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        provider: DocumentPort = self.providers.get(DocumentPort)
        result = await provider.execute(
            self.operation,
            context.envelope.principal,
            arguments.model_dump(mode="json"),
            idempotency_key=context.idempotency_key,
        )
        return _provider_result(f"document.{self.operation}d", result)


def _provider_result(code: str, result: ProviderCallResult) -> IntentResult:
    data = dict(result.data)
    if result.remote_resource_id:
        data["resource_id"] = result.remote_resource_id
    return IntentResult.success(code=code, data=data)


def _spec(
    *,
    module_id: str,
    intent_id: str,
    description: str,
    arguments_model: type[BaseModel],
    handler: Any,
    operation: OperationKind,
    permission: str,
    agent_exposed: bool = True,
    unknown_resolution_policy: UnknownResolutionPolicy | None = None,
    reconcile: Any = None,
) -> IntentSpec:
    high = operation in {OperationKind.DELETE, OperationKind.OVERWRITE, OperationKind.BULK}
    return IntentSpec(
        intent_id=intent_id,
        version=1,
        module_id=module_id,
        description=description,
        arguments_model=arguments_model,
        result_model=IntentResult,
        handler=handler,
        agent_exposed=agent_exposed,
        operation_kind=operation,
        risk_level=RiskLevel.HIGH if high else (
            RiskLevel.MEDIUM if operation is OperationKind.UPDATE else RiskLevel.LOW
        ),
        required_permissions=frozenset({permission}),
        idempotency_policy=(
            IdempotencyPolicy.NONE if operation is OperationKind.READ else IdempotencyPolicy.REQUIRED
        ),
        confirmation_policy=ConfirmationPolicy(required=high),
        unknown_resolution_policy=unknown_resolution_policy
        or (
            UnknownResolutionPolicy.NONE
            if operation is OperationKind.READ
            else UnknownResolutionPolicy.RETRY_IDEMPOTENT
        ),
        reconcile=reconcile,
    )


class CalendarModule:
    manifest = ModuleManifest(
        module_id="calendar", version="1.0.0", owns_namespaces=frozenset({"calendar"})
    )

    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    def contribute(self, sink: ContributionSink) -> None:
        definitions = (
            ("query", "Query calendar events.", CalendarQueryArguments, OperationKind.READ),
            ("create", "Create a calendar event.", CalendarCreateArguments, OperationKind.CREATE),
            ("update", "Update a calendar event.", CalendarUpdateArguments, OperationKind.UPDATE),
            ("delete", "Delete a calendar event.", CalendarDeleteArguments, OperationKind.DELETE),
        )
        for name, description, model, operation in definitions:
            handler = CalendarHandler(self.providers, name)
            sink.add_intent(
                _spec(
                    module_id="calendar",
                    intent_id=f"calendar.{name}",
                    description=description,
                    arguments_model=model,
                    handler=handler,
                    operation=operation,
                    permission=f"calendar.{'read' if operation is OperationKind.READ else 'write'}",
                    unknown_resolution_policy=(
                        UnknownResolutionPolicy.NONE
                        if operation is OperationKind.READ
                        else UnknownResolutionPolicy.RECONCILE_THEN_RETRY
                    ),
                    reconcile=handler.reconcile if operation is not OperationKind.READ else None,
                )
            )


class TaskModule:
    manifest = ModuleManifest(
        module_id="task", version="1.0.0", owns_namespaces=frozenset({"task"})
    )

    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    def contribute(self, sink: ContributionSink) -> None:
        definitions = (
            ("query", "Query tasks.", TaskQueryArguments, OperationKind.READ),
            ("create", "Create a task.", TaskCreateArguments, OperationKind.CREATE),
            ("update", "Update a task.", TaskUpdateArguments, OperationKind.UPDATE),
            ("delete", "Delete a task.", TaskDeleteArguments, OperationKind.DELETE),
        )
        for name, description, model, operation in definitions:
            sink.add_intent(
                _spec(
                    module_id="task",
                    intent_id=f"task.{name}",
                    description=description,
                    arguments_model=model,
                    handler=TaskHandler(self.providers, name),
                    operation=operation,
                    permission=f"task.{'read' if operation is OperationKind.READ else 'write'}",
                )
            )


class ReminderModule:
    manifest = ModuleManifest(
        module_id="reminder", version="1.0.0", owns_namespaces=frozenset({"reminder"})
    )

    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    def contribute(self, sink: ContributionSink) -> None:
        sink.add_intent(
            _spec(
                module_id="reminder",
                intent_id="reminder.create",
                description="Create a reminder represented by the configured task provider.",
                arguments_model=ReminderCreateArguments,
                handler=TaskHandler(self.providers, "reminder_create"),
                operation=OperationKind.CREATE,
                permission="task.write",
            )
        )


class DocumentModule:
    manifest = ModuleManifest(
        module_id="document", version="1.0.0", owns_namespaces=frozenset({"document"})
    )

    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    def contribute(self, sink: ContributionSink) -> None:
        definitions = (
            ("query", "Search or read documents.", DocumentQueryArguments, OperationKind.READ),
            ("create", "Create a document.", DocumentCreateArguments, OperationKind.CREATE),
            ("update", "Append to or replace a document.", DocumentUpdateArguments, OperationKind.OVERWRITE),
            ("delete", "Delete a document.", DocumentDeleteArguments, OperationKind.DELETE),
        )
        for name, description, model, operation in definitions:
            sink.add_intent(
                _spec(
                    module_id="document",
                    intent_id=f"document.{name}",
                    description=description,
                    arguments_model=model,
                    handler=DocumentHandler(self.providers, name),
                    operation=operation,
                    permission=f"document.{'read' if operation is OperationKind.READ else 'write'}",
                )
            )
