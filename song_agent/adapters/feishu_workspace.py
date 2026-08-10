from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from ..kernel.errors import DeliveryState, ProviderError
from ..kernel.models import PrincipalIdentity
from ..ports.credentials import FeishuTokenPort
from ..ports.workspace import ProviderCallResult


class FeishuWorkspaceProvider:
    """Neutral workspace ports translated to Feishu OpenAPI at the adapter boundary."""

    def __init__(
        self,
        *,
        base_url: str,
        user_access_token: str = "",
        tenant_id: str = "",
        principal_id: str = "",
        token_provider: FeishuTokenPort | None = None,
        calendar_id: str = "",
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_access_token = user_access_token
        self.tenant_id = tenant_id
        self.principal_id = principal_id
        self.token_provider = token_provider
        self._active_token: ContextVar[str] = ContextVar("feishu_access_token", default="")
        self.calendar_id = calendar_id
        self.timeout = timeout
        self.transport = transport

    async def create(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult:
        await self._check_principal(principal)
        calendar_id = await self._calendar_id()
        body: dict[str, Any] = {
            "summary": arguments["summary"],
            "description": arguments.get("description", ""),
            "start_time": _calendar_time(arguments["start_time"], arguments["timezone"]),
            "end_time": _calendar_time(arguments["end_time"], arguments["timezone"]),
            "need_notification": True,
        }
        if arguments.get("location"):
            body["location"] = {"name": arguments["location"]}
        data, request_id = await self._request(
            "POST",
            f"/open-apis/calendar/v4/calendars/{quote(calendar_id, safe='')}/events",
            params={"user_id_type": "open_id", "idempotency_key": idempotency_key},
            json_body=body,
            write=True,
        )
        event = data.get("event") if isinstance(data.get("event"), dict) else data
        event_id = str(event.get("event_id") or "")
        attendees = arguments.get("attendee_ids") or []
        if attendees and event_id:
            await self._request(
                "POST",
                (
                    f"/open-apis/calendar/v4/calendars/{quote(calendar_id, safe='')}"
                    f"/events/{quote(event_id, safe='')}/attendees"
                ),
                params={"user_id_type": "open_id"},
                json_body={
                    "attendees": [
                        {"type": "user", "user_id": attendee} for attendee in attendees
                    ],
                    "need_notification": True,
                },
                write=True,
            )
        return ProviderCallResult(
            data=event,
            provider_request_id=request_id,
            remote_resource_id=event_id,
        )

    async def query(
        self, principal: PrincipalIdentity, arguments: dict[str, Any]
    ) -> ProviderCallResult:
        await self._check_principal(principal)
        calendar_id = await self._calendar_id()
        if arguments.get("event_id"):
            path = (
                f"/open-apis/calendar/v4/calendars/{quote(calendar_id, safe='')}"
                f"/events/{quote(arguments['event_id'], safe='')}"
            )
            data, request_id = await self._request(
                "GET", path, params={"user_id_type": "open_id"}
            )
        elif arguments.get("query"):
            data, request_id = await self._request(
                "POST",
                f"/open-apis/calendar/v4/calendars/{quote(calendar_id, safe='')}/events/search",
                params={
                    "page_size": arguments.get("page_size", 20),
                    "user_id_type": "open_id",
                },
                json_body={"query": arguments["query"]},
            )
        else:
            params = {
                "start_time": _unix(arguments.get("start_time") or datetime.now().isoformat()),
                "end_time": _unix(arguments.get("end_time") or "2099-01-01T00:00:00+00:00"),
                "user_id_type": "open_id",
            }
            data, request_id = await self._request(
                "GET",
                (
                    f"/open-apis/calendar/v4/calendars/{quote(calendar_id, safe='')}"
                    "/events/instance_view"
                ),
                params=params,
            )
        return ProviderCallResult(data=data, provider_request_id=request_id)

    async def update(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult:
        await self._check_principal(principal)
        calendar_id = await self._calendar_id()
        event_id = arguments["event_id"]
        data, request_id = await self._request(
            "PATCH",
            (
                f"/open-apis/calendar/v4/calendars/{quote(calendar_id, safe='')}"
                f"/events/{quote(event_id, safe='')}"
            ),
            params={"user_id_type": "open_id", "idempotency_key": idempotency_key},
            json_body={**arguments["fields"], "need_notification": True},
            write=True,
        )
        return ProviderCallResult(
            data=data, provider_request_id=request_id, remote_resource_id=event_id
        )

    async def delete(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult:
        await self._check_principal(principal)
        calendar_id = arguments.get("calendar_id") or await self._calendar_id()
        event_id = arguments["event_id"]
        _, request_id = await self._request(
            "DELETE",
            (
                f"/open-apis/calendar/v4/calendars/{quote(calendar_id, safe='')}"
                f"/events/{quote(event_id, safe='')}"
            ),
            params={"need_notification": "true", "idempotency_key": idempotency_key},
            write=True,
        )
        return ProviderCallResult(
            data={"event_id": event_id, "deleted": True},
            provider_request_id=request_id,
            remote_resource_id=event_id,
        )

    async def reconcile(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult | None:
        if not arguments.get("event_id"):
            return None
        try:
            return await self.query(
                principal,
                {"event_id": arguments["event_id"], "page_size": 1},
            )
        except ProviderError as error:
            if error.error_code in {"feishu.404", "feishu.230001"}:
                return ProviderCallResult(data={"exists": False})
            raise

    async def execute(
        self,
        operation: str,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult:
        await self._check_principal(principal)
        if operation.startswith("reminder_"):
            arguments = {
                "summary": arguments["summary"],
                "description": arguments.get("description", ""),
                "due_time": arguments["remind_at"],
            }
            operation = operation.removeprefix("reminder_")
        if operation == "query":
            return await self._query_tasks(arguments)
        if operation == "create":
            body = {
                "summary": arguments["summary"],
                "description": arguments.get("description", ""),
                "client_token": idempotency_key,
            }
            if arguments.get("start_time"):
                body["start"] = _task_time(arguments["start_time"])
            if arguments.get("due_time"):
                body["due"] = _task_time(arguments["due_time"])
            if arguments.get("assignee_ids"):
                body["members"] = [
                    {"id": item, "type": "user", "role": "assignee"}
                    for item in arguments["assignee_ids"]
                ]
            data, request_id = await self._request(
                "POST",
                "/open-apis/task/v2/tasks",
                params={"user_id_type": "open_id"},
                json_body=body,
                write=True,
            )
            task = data.get("task") if isinstance(data.get("task"), dict) else data
            task_id = str(task.get("guid") or task.get("task_guid") or "")
            return ProviderCallResult(task, request_id, task_id)
        task_id = arguments["task_id"]
        if operation == "delete":
            _, request_id = await self._request(
                "DELETE",
                f"/open-apis/task/v2/tasks/{quote(task_id, safe='')}",
                params={"idempotency_key": idempotency_key},
                write=True,
            )
            return ProviderCallResult({"task_id": task_id, "deleted": True}, request_id, task_id)
        data, request_id = await self._request(
            "PATCH",
            f"/open-apis/task/v2/tasks/{quote(task_id, safe='')}",
            params={"user_id_type": "open_id", "idempotency_key": idempotency_key},
            json_body={
                "task": arguments["fields"],
                "update_fields": sorted(arguments["fields"]),
            },
            write=True,
        )
        return ProviderCallResult(data, request_id, task_id)

    async def execute_document(
        self,
        operation: str,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult:
        await self._check_principal(principal)
        if operation == "query":
            if arguments.get("document_id"):
                data, request_id = await self._request(
                    "GET",
                    (
                        f"/open-apis/docx/v1/documents/"
                        f"{quote(arguments['document_id'], safe='')}/raw_content"
                    ),
                )
            else:
                data, request_id = await self._request(
                    "POST",
                    "/open-apis/search/v2/doc_wiki/search",
                    json_body={
                        "query": arguments.get("query", ""),
                        "page_size": arguments.get("page_size", 20),
                    },
                )
            return ProviderCallResult(data, request_id)
        if operation == "create":
            data, request_id = await self._request(
                "POST",
                "/open-apis/docx/v1/documents",
                params={"idempotency_key": idempotency_key},
                json_body={"title": arguments["title"]},
                write=True,
            )
            document = data.get("document") if isinstance(data.get("document"), dict) else data
            document_id = str(document.get("document_id") or "")
            if arguments.get("content") and document_id:
                await self._append_document(document_id, arguments["content"])
            return ProviderCallResult(document, request_id, document_id)
        document_id = arguments["document_id"]
        if operation == "delete":
            _, request_id = await self._request(
                "DELETE",
                f"/open-apis/drive/v1/files/{quote(document_id, safe='')}",
                params={"type": "docx", "idempotency_key": idempotency_key},
                write=True,
            )
            return ProviderCallResult(
                {"document_id": document_id, "deleted": True}, request_id, document_id
            )
        if arguments.get("mode") == "replace":
            raise ProviderError(
                "feishu.document_replace_not_supported",
                "Feishu document replacement requires a provider with atomic replace support.",
                delivery_state=DeliveryState.NOT_SENT,
            )
        request_id = await self._append_document(document_id, arguments.get("content", ""))
        return ProviderCallResult(
            {"document_id": document_id, "updated": True}, request_id, document_id
        )

    async def _query_tasks(self, arguments: dict[str, Any]) -> ProviderCallResult:
        if arguments.get("task_id"):
            data, request_id = await self._request(
                "GET", f"/open-apis/task/v2/tasks/{quote(arguments['task_id'], safe='')}"
            )
        else:
            params: dict[str, Any] = {
                "type": "my_tasks",
                "page_size": arguments.get("page_size", 20),
                "user_id_type": "open_id",
            }
            if arguments.get("completed") is not None:
                params["completed"] = str(arguments["completed"]).lower()
            data, request_id = await self._request(
                "GET", "/open-apis/task/v2/tasks", params=params
            )
            if arguments.get("query") and isinstance(data.get("items"), list):
                query = arguments["query"].casefold()
                data["items"] = [
                    item
                    for item in data["items"]
                    if query in str(item.get("summary") or "").casefold()
                ]
        return ProviderCallResult(data, request_id)

    async def _calendar_id(self) -> str:
        if self.calendar_id:
            return self.calendar_id
        data, _ = await self._request("GET", "/open-apis/calendar/v4/calendars/primary")
        calendar = data.get("calendars") or data.get("calendar") or data
        if isinstance(calendar, list):
            calendar = calendar[0] if calendar else {}
        calendar_id = str(calendar.get("calendar_id") or "") if isinstance(calendar, dict) else ""
        if not calendar_id:
            raise ProviderError(
                "feishu.calendar_id_missing",
                "Feishu did not return a primary calendar id.",
                delivery_state=DeliveryState.REJECTED,
            )
        return calendar_id

    async def _append_document(self, document_id: str, content: str) -> str:
        _, request_id = await self._request(
            "POST",
            (
                f"/open-apis/docx/v1/documents/{quote(document_id, safe='')}"
                f"/blocks/{quote(document_id, safe='')}/children"
            ),
            json_body={
                "index": 0,
                "children": [
                    {
                        "block_type": 2,
                        "text": {"elements": [{"text_run": {"content": content}}]},
                    }
                ],
            },
            write=True,
        )
        return request_id

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        write: bool = False,
    ) -> tuple[dict[str, Any], str]:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {self._active_token.get()}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise ProviderError(
                "feishu.not_sent",
                "Unable to connect to Feishu.",
                retryable=True,
                delivery_state=DeliveryState.NOT_SENT,
            ) from error
        except (httpx.ReadTimeout, httpx.RemoteProtocolError) as error:
            raise ProviderError(
                "feishu.response_unknown" if write else "feishu.read_failed",
                "Feishu did not return a verifiable response.",
                retryable=not write,
                delivery_state=DeliveryState.UNKNOWN if write else DeliveryState.NOT_SENT,
            ) from error
        request_id = response.headers.get("x-tt-logid", "")
        try:
            payload = response.json() if response.content else {}
        except ValueError as error:
            raise ProviderError(
                "feishu.invalid_response",
                f"Feishu returned HTTP {response.status_code} with an invalid body.",
                retryable=response.status_code >= 500,
                delivery_state=DeliveryState.UNKNOWN if write else DeliveryState.REJECTED,
                provider_request_id=request_id,
            ) from error
        raw_code = payload.get("code") or (response.status_code if response.is_error else 0)
        code = str(raw_code)
        if response.is_error or code != "0":
            raise ProviderError(
                f"feishu.{code}",
                str(payload.get("msg") or f"Feishu request failed: HTTP {response.status_code}"),
                retryable=response.status_code == 429 or response.status_code >= 500,
                delivery_state=DeliveryState.REJECTED,
                provider_request_id=request_id,
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return data, request_id

    async def _check_principal(self, principal: PrincipalIdentity) -> None:
        if self.token_provider is not None:
            self._active_token.set(await self.token_provider.access_token(principal))
            return
        if not self.user_access_token or (
            principal.tenant_id != self.tenant_id
            or principal.principal_id != self.principal_id
        ):
            raise ProviderError(
                "feishu.principal_not_bound",
                "The authenticated principal has no Feishu credential binding.",
                delivery_state=DeliveryState.NOT_SENT,
            )
        self._active_token.set(self.user_access_token)


class FeishuDocumentProvider:
    def __init__(self, workspace: FeishuWorkspaceProvider) -> None:
        self.workspace = workspace

    async def execute(
        self,
        operation: str,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult:
        return await self.workspace.execute_document(
            operation,
            principal,
            arguments,
            idempotency_key=idempotency_key,
        )


def _unix(value: str) -> str:
    return str(int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()))


def _calendar_time(value: str, timezone: str) -> dict[str, str]:
    return {"timestamp": _unix(value), "timezone": timezone}


def _task_time(value: str) -> dict[str, str | bool]:
    timestamp = int(
        datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    )
    return {"timestamp": str(timestamp), "is_all_day": False}
