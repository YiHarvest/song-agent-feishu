from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..kernel.models import PrincipalIdentity


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    data: dict[str, Any]
    provider_request_id: str = ""
    remote_resource_id: str = ""


class CalendarPort(Protocol):
    async def create(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult: ...

    async def query(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
    ) -> ProviderCallResult: ...

    async def update(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult: ...

    async def delete(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult: ...

    async def reconcile(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult | None: ...


class TaskPort(Protocol):
    async def execute(
        self,
        operation: str,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult: ...


class DocumentPort(Protocol):
    async def execute(
        self,
        operation: str,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ProviderCallResult: ...
