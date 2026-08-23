from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from threading import Lock

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from control_center.app import AUTH_KEY, create_control_app
from control_center.auth import CONTROL_CSRF_HEADER
from control_center.memory_api import mount_memory_api
from conversation_memory_admin import (
    ConversationMemoryAdminError,
    MemoryAdminMutationResult,
    MemoryAdminMutationStatus,
    MemoryAdminStatus,
)
from conversation_memory_port import ConversationMemoryRecord
from private_world_ledger import LedgerEvent
from private_world_port import PrivateWorldSnapshot


NOW = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)


class FakeLedger:
    def __init__(self) -> None:
        self.current = PrivateWorldSnapshot()
        self.items: list[LedgerEvent] = []
        self._lock = Lock()

    def snapshot(self) -> PrivateWorldSnapshot:
        return self.current

    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self.items)

    def apply_once(
        self,
        event: LedgerEvent,
        snapshot: PrivateWorldSnapshot,
    ) -> bool:
        with self._lock:
            if any(
                row.event_id == event.event_id
                or row.delivery_id == event.delivery_id
                for row in self.items
            ):
                return False
            self.items.append(event)
            self.current = snapshot
            return True


class RecordingMemoryBackend:
    def __init__(self) -> None:
        self.record = ConversationMemoryRecord(
            memory_id="memory-1",
            text="用户现在住在东京。<script>not executable</script>",
            user_id="private-user-scope",
            source_id="reply:letter-1:1",
            score=0.91,
            occurred_at=NOW,
            created_at=NOW,
            metadata={"manual": False, "actor": "provider"},
        )
        self.calls: list[tuple[str, object]] = []
        self.fail = False

    def _maybe_fail(self) -> None:
        if self.fail:
            raise OSError("C:/private/memory/qdrant")

    def list_memories(self, *, query=None, limit=100):
        self._maybe_fail()
        self.calls.append(("list", (query, limit)))
        if query and "东京" not in query:
            return ()
        return (self.record,)[:limit]

    def status(self) -> MemoryAdminStatus:
        self._maybe_fail()
        return MemoryAdminStatus(
            "available",
            "mem0",
            True,
            1,
            2,
            0,
        )

    def add(self, text, *, request_id, reason):
        self._maybe_fail()
        self.calls.append(("add", (text, request_id, reason)))
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.APPLIED,
            request_id,
            "add",
            affected_count=1,
            replacement_memory_id="memory-added",
        )

    def correct(self, memory_id, corrected_text, *, request_id, reason):
        self._maybe_fail()
        self.calls.append(
            ("correct", (memory_id, corrected_text, request_id, reason))
        )
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.APPLIED,
            request_id,
            "correct",
            affected_count=2,
            target_memory_id=memory_id,
            replacement_memory_id="memory-corrected",
        )

    def delete(self, memory_id, *, request_id, reason):
        self._maybe_fail()
        self.calls.append(("delete", (memory_id, request_id, reason)))
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.APPLIED,
            request_id,
            "delete",
            affected_count=1,
            target_memory_id=memory_id,
        )

    def clear(self, *, request_id, reason, confirmed):
        self._maybe_fail()
        self.calls.append(("clear", (request_id, reason, confirmed)))
        if not confirmed:
            raise ConversationMemoryAdminError(
                "MEMORY_ADMIN_CONFIRMATION_REQUIRED"
            )
        return MemoryAdminMutationResult(
            MemoryAdminMutationStatus.APPLIED,
            request_id,
            "clear",
            affected_count=1,
        )

    def export(self):
        self._maybe_fail()
        return {
            "schema_version": "p03.conversation-memory-export.v1",
            "provider": "mem0",
            "user_id": "private-user-scope",
            "private_world": "must-not-cross",
            "provider_config": {"api_key": "must-not-cross"},
            "records": [
                {
                    **self.record.to_prompt_dict(),
                    "user_id": "private-user-scope",
                    "metadata": {"private": True},
                }
            ],
        }


async def _authenticated_client(
    backend: RecordingMemoryBackend,
) -> tuple[TestClient, str, str]:
    app = create_control_app(FakeLedger())
    mount_memory_api(app, backend)
    client = TestClient(
        TestServer(app),
        cookie_jar=CookieJar(unsafe=True),
    )
    await client.start_server()
    origin = str(client.make_url("/")).rstrip("/")
    token = app[AUTH_KEY].issue_bootstrap_token()
    response = await client.post(
        "/control/api/session/bootstrap",
        json={"token": token},
        headers={"Origin": origin},
    )
    assert response.status == 200
    csrf = (await response.json())["data"]["csrf_token"]
    return client, origin, csrf


def test_memory_status_list_and_search_use_shared_auth_boundary() -> None:
    async def scenario() -> None:
        backend = RecordingMemoryBackend()
        app = create_control_app(FakeLedger())
        mount_memory_api(app, backend)
        async with TestClient(TestServer(app)) as unauthenticated:
            denied = await unauthenticated.get("/control/api/memory/status")
            assert denied.status == 401
            assert (await denied.json())["error"]["code"] == (
                "CONTROL_SESSION_REQUIRED"
            )

        client, origin, csrf = await _authenticated_client(backend)
        try:
            status = await client.get("/control/api/memory/status")
            assert status.status == 200
            status_payload = (await status.json())["data"]
            assert status_payload["provider"] == "mem0"
            assert status_payload["memory_count"] == 1

            listed = await client.get("/control/api/memory?limit=20")
            assert listed.status == 200
            payload = (await listed.json())["data"]
            assert payload["count"] == 1
            assert payload["memories"][0]["text"].startswith(
                "用户现在住在东京"
            )
            assert "user_id" not in payload["memories"][0]
            assert "metadata" not in payload["memories"][0]

            searched = await client.post(
                "/control/api/memory/search",
                json={"query": "东京", "limit": 5},
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert searched.status == 200
            assert (await searched.json())["data"]["count"] == 1
            assert backend.calls[-1] == ("list", ("东京", 5))
        finally:
            await client.close()

    asyncio.run(scenario())


def test_memory_mutations_require_csrf_and_delegate_strict_payloads() -> None:
    async def scenario() -> None:
        backend = RecordingMemoryBackend()
        client, origin, csrf = await _authenticated_client(backend)
        try:
            missing_csrf = await client.post(
                "/control/api/memory/manual",
                json={
                    "request_id": "memory.add-1",
                    "text": "用户喜欢黑胶。",
                    "reason": "用户明确要求记住。",
                },
                headers={"Origin": origin},
            )
            assert missing_csrf.status == 403
            assert (await missing_csrf.json())["error"]["code"] == (
                "CONTROL_CSRF_REQUIRED"
            )

            added = await client.post(
                "/control/api/memory/manual",
                json={
                    "request_id": "memory.add-1",
                    "text": "用户喜欢黑胶。",
                    "reason": "用户明确要求记住。",
                },
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert added.status == 200
            assert (await added.json())["data"]["replacement_memory_id"] == (
                "memory-added"
            )

            corrected = await client.post(
                "/control/api/memory/memory-1/correct",
                json={
                    "request_id": "memory.correct-1",
                    "text": "用户现在住在横滨。",
                    "reason": "用户纠正了居住地。",
                },
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert corrected.status == 200
            assert (await corrected.json())["data"]["affected_count"] == 2

            deleted = await client.delete(
                "/control/api/memory/memory-1",
                json={
                    "request_id": "memory.delete-1",
                    "reason": "用户确认删除错误记忆。",
                },
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert deleted.status == 200
            assert (await deleted.json())["data"]["operation"] == "delete"

            unconfirmed = await client.post(
                "/control/api/memory/clear",
                json={
                    "request_id": "memory.clear-1",
                    "reason": "用户申请清空新对话记忆。",
                    "confirmed": False,
                },
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert unconfirmed.status == 409
            assert (await unconfirmed.json())["error"]["code"] == (
                "MEMORY_ADMIN_CONFIRMATION_REQUIRED"
            )

            cleared = await client.post(
                "/control/api/memory/clear",
                json={
                    "request_id": "memory.clear-2",
                    "reason": "用户确认清空新对话记忆。",
                    "confirmed": True,
                },
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert cleared.status == 200
            assert (await cleared.json())["data"]["operation"] == "clear"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_memory_export_is_authenticated_and_strips_scope_and_control_fields() -> None:
    async def scenario() -> None:
        backend = RecordingMemoryBackend()
        client, origin, csrf = await _authenticated_client(backend)
        try:
            response = await client.post(
                "/control/api/memory/export",
                json={},
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert response.status == 200
            payload = (await response.json())["data"]["export"]
            assert payload["provider"] == "mem0"
            assert payload["records"][0]["memory_id"] == "memory-1"
            encoded = repr(payload)
            assert "private-user-scope" not in encoded
            assert "private_world" not in encoded
            assert "provider_config" not in encoded
            assert "api_key" not in encoded
            assert "metadata" not in payload["records"][0]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_memory_api_rejects_extra_fields_and_returns_path_free_backend_errors() -> None:
    async def scenario() -> None:
        backend = RecordingMemoryBackend()
        client, origin, csrf = await _authenticated_client(backend)
        try:
            extra = await client.post(
                "/control/api/memory/manual",
                json={
                    "request_id": "memory.add-extra",
                    "text": "测试记忆。",
                    "reason": "测试。",
                    "private_world": "forbidden",
                },
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert extra.status == 400
            assert (await extra.json())["error"]["code"] == (
                "CONTROL_BODY_FIELDS_INVALID"
            )

            backend.fail = True
            failed = await client.get("/control/api/memory/status")
            assert failed.status == 503
            payload = await failed.json()
            assert payload == {
                "ok": False,
                "error": {"code": "MEMORY_CONTROL_UNAVAILABLE"},
            }
            assert "C:/private" not in repr(payload)
        finally:
            await client.close()

    asyncio.run(scenario())
