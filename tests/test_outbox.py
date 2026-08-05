from __future__ import annotations

from pathlib import Path

import pytest

from song_agent.models import PendingAction
from song_agent.services.audit import AuditService
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.services.outbox import ActionOutboxWorker
from song_agent.services.pending_actions import PendingActionService
from song_agent.services.reconciliation import ActionReconciliationService
from song_agent.store import SqliteStore
from tests.test_pending_actions import message


async def _store(path: Path) -> SqliteStore:
    store = SqliteStore(
        path,
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_outbox_executes_confirmed_action_after_callback_process_is_gone(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path / "state.db")
    try:
        action = await PendingActionService(store).create_action(
            message(),
            action_type="calendar.create",
            payload={"summary": "开会", "start_time": "2026-07-24T10:00:00+08:00"},
            idempotency_key="outbox-test",
            source="test",
        )
        assert await store.claim_pending_action(
            action.action_id,
            actor_open_id="open-a",
            payload_hash=action.payload_hash,
        )
        executed: list[str] = []

        async def execute(candidate: PendingAction, context: object) -> None:
            del context
            if await store.claim_action_execution(candidate.action_id, worker_id="outbox"):
                executed.append(candidate.action_id)
                await store.finish_pending_action(candidate.action_id, success=True)

        async def reconcile(candidate: PendingAction) -> bool:
            del candidate
            return False

        worker = ActionOutboxWorker(store, execute, reconcile)
        await worker.run_once()

        assert executed == [action.action_id]
        stored = await store.get_pending_action(action.action_id)
        assert stored and stored.status == "succeeded"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expired_execution_becomes_unknown_and_is_not_blindly_retried(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path / "state.db")
    try:
        action = await PendingActionService(store).create_action(
            message(),
            action_type="calendar.create",
            payload={"summary": "开会", "start_time": "2026-07-24T10:00:00+08:00"},
            idempotency_key="outbox-test",
            source="test",
        )
        assert await store.claim_pending_action(
            action.action_id,
            actor_open_id="open-a",
            payload_hash=action.payload_hash,
        )
        assert await store.claim_action_execution(
            action.action_id,
            worker_id="crashed-worker",
            lease_seconds=-1,
        )

        assert await store.recover_expired_action_claims() == 1
        stored = await store.get_pending_action(action.action_id)
        assert stored and stored.status == "unknown_remote_state"
        assert await store.ready_outbox_action_ids() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reconciliation_completes_document_with_durable_remote_id(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path / "state.db")
    try:
        action = await PendingActionService(store).create_document_action(
            message(),
            action_type="document.create",
            title="可靠文档",
            markdown="body",
        )
        assert await store.claim_pending_action(
            action.action_id,
            actor_open_id="open-a",
            payload_hash=action.payload_hash,
        )
        assert await store.claim_action_execution(action.action_id, worker_id="worker")
        assert await store.record_action_remote_success(
            action.action_id,
            remote_resource_id="docx-remote-id",
        )
        assert await store.mark_action_unknown(
            action.action_id,
            error_code="local_commit_failed",
            error_message="simulated crash",
        )
        unknown = await store.get_pending_action(action.action_id)
        assert unknown is not None
        service = ActionReconciliationService(store, AuditService(store))

        assert await service.reconcile(unknown)
        stored = await store.get_pending_action(action.action_id)
        binding = await store.get_document_binding(
            "chat",
            "union-a",
            tenant_key="tenant",
            app_id="app",
            thread_id="thread",
        )
        assert stored and stored.status == "succeeded"
        assert binding and binding.token == "docx-remote-id"
    finally:
        await store.close()
