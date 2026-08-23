from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from control_center.private_world_api import PrivateWorldControlAPI
from original_client_companion_mutation_api import (
    CONFIRM_HEADER,
    CONFIRM_VALUE,
    CompanionMutationResult,
    PRIVATE_WORLD_CONTINUATION_PATH,
    PRIVATE_WORLD_HOME_ACCESS_PATH,
    PRIVATE_WORLD_NICKNAME_PATH,
    mount_original_client_companion_mutation_api,
)
from original_client_companion_mutation_backend import (
    DirectOriginalClientCompanionMutationBackend,
    DirectOriginalClientPrivateWorldMutationBackend,
)
from original_client_server import (
    create_configured_original_client_server_runtime,
)
from private_world_delivery import PrivateWorldDeliveryCommitter
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import PrivateWorldCommandService


TRUSTED_ORIGIN = "https://client.example"


class NoopCompanionBackend:
    def correct_memory(self, **kwargs) -> CompanionMutationResult:
        raise AssertionError(kwargs)

    def delete_memory(self, **kwargs) -> CompanionMutationResult:
        raise AssertionError(kwargs)

    def decide_candidate(self, **kwargs) -> CompanionMutationResult:
        raise AssertionError(kwargs)


class RecordingPrivateWorldBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute_private_world(self, **kwargs) -> CompanionMutationResult:
        self.calls.append(kwargs)
        return CompanionMutationResult(
            request_id=str(kwargs["request_id"]),
            status="APPLIED",
            affected_count=1,
            reason_code="PRIVATE_WORLD_COMMAND_APPLIED",
        )


async def _transport_client():
    app = web.Application()
    private_backend = RecordingPrivateWorldBackend()
    mount_original_client_companion_mutation_api(
        app,
        NoopCompanionBackend(),
        private_world_backend=private_backend,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    origin = str(client.make_url("/")).rstrip("/")
    return client, private_backend, origin


def _headers(origin: str) -> dict[str, str]:
    return {
        "Origin": origin,
        CONFIRM_HEADER: CONFIRM_VALUE,
    }


def test_transport_delegates_nickname_home_and_continuation() -> None:
    async def scenario() -> None:
        client, backend, origin = await _transport_client()
        try:
            granted = await client.post(
                PRIVATE_WORLD_NICKNAME_PATH,
                json={
                    "action": "grant",
                    "nickname": "小离",
                    "request_id": "request.nickname.grant.1",
                    "reason": "用户明确授权私人称呼。",
                    "occurred_at": "2026-08-23T12:00:00+00:00",
                },
                headers=_headers(origin),
            )
            assert granted.status == 200
            home = await client.post(
                PRIVATE_WORLD_HOME_ACCESS_PATH,
                json={
                    "home_access": "visit_access",
                    "request_id": "request.home.visit.1",
                    "reason": "用户明确授权到访。",
                    "occurred_at": "2026-08-23T12:01:00+00:00",
                },
                headers=_headers(origin),
            )
            assert home.status == 200
            continuation = await client.post(
                PRIVATE_WORLD_CONTINUATION_PATH,
                json={
                    "action": "upsert",
                    "fact_id": "continuation.fixture.1",
                    "statement": "林离知道用户已经搬到东京。",
                    "awareness": "pending",
                    "confirm_character_known": False,
                    "request_id": "request.continuation.upsert.1",
                    "reason": "用户新增本地世界线。",
                    "occurred_at": "2026-08-23T12:02:00+00:00",
                },
                headers=_headers(origin),
            )
            assert continuation.status == 200
            assert [call["operation"] for call in backend.calls] == [
                "nickname",
                "home_access",
                "continuation",
            ]
            assert backend.calls[2]["payload"] == {
                "action": "upsert",
                "fact_id": "continuation.fixture.1",
                "statement": "林离知道用户已经搬到东京。",
                "awareness": "pending",
            }
        finally:
            await client.close()

    asyncio.run(scenario())


def test_transport_requires_character_known_confirmation_and_exact_fields() -> None:
    async def scenario() -> None:
        client, backend, origin = await _transport_client()
        try:
            unconfirmed = await client.post(
                PRIVATE_WORLD_CONTINUATION_PATH,
                json={
                    "action": "set_awareness",
                    "fact_id": "continuation.fixture.1",
                    "awareness": "character_known",
                    "confirm_character_known": False,
                    "request_id": "request.continuation.known.1",
                    "reason": "用户确认林离已经知道。",
                    "occurred_at": "2026-08-23T12:03:00+00:00",
                },
                headers=_headers(origin),
            )
            assert unconfirmed.status == 403
            assert (await unconfirmed.json())["error_code"] == (
                "PRIVATE_WORLD_CHARACTER_KNOWN_CONFIRMATION_REQUIRED"
            )
            hidden = await client.post(
                PRIVATE_WORLD_NICKNAME_PATH,
                json={
                    "action": "grant",
                    "nickname": "小离",
                    "request_id": "request.nickname.hidden.1",
                    "reason": "fixture",
                    "occurred_at": "2026-08-23T12:04:00+00:00",
                    "trust_score": 100,
                },
                headers=_headers(origin),
            )
            assert hidden.status == 400
            missing_header = await client.post(
                PRIVATE_WORLD_HOME_ACCESS_PATH,
                json={
                    "home_access": "visit_access",
                    "request_id": "request.home.no-confirm.1",
                    "reason": "fixture",
                    "occurred_at": "2026-08-23T12:05:00+00:00",
                },
                headers={"Origin": origin},
            )
            assert missing_header.status == 403
            assert backend.calls == []
        finally:
            await client.close()

    asyncio.run(scenario())


def test_direct_backend_reuses_canonical_service_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ledger = SQLitePrivateWorldLedger(tmp_path / "private_world.sqlite3")
    backend = DirectOriginalClientPrivateWorldMutationBackend(
        PrivateWorldControlAPI(
            ledger,
            PrivateWorldCommandService(ledger),
        )
    )
    first = backend.execute_private_world(
        operation="nickname",
        payload={"action": "grant", "nickname": "小离"},
        request_id="request.nickname.backend.1",
        reason="用户明确授权。",
        occurred_at="2026-08-23T12:00:00+00:00",
    )
    duplicate = backend.execute_private_world(
        operation="nickname",
        payload={"action": "grant", "nickname": "小离"},
        request_id="request.nickname.backend.1",
        reason="用户明确授权。",
        occurred_at="2026-08-23T12:00:00+00:00",
    )
    assert first.status == "APPLIED"
    assert duplicate.status == "DUPLICATE"
    assert ledger.snapshot().nickname_permissions == ("小离",)
    assert len(ledger.events()) == 1


def test_direct_backend_disabled_and_unknown_operation_fail_closed() -> None:
    disabled = DirectOriginalClientPrivateWorldMutationBackend(None)
    with pytest.raises(Exception, match="PRIVATE_WORLD_MUTATION_DISABLED"):
        disabled.execute_private_world(
            operation="nickname",
            payload={"action": "grant", "nickname": "小离"},
            request_id="request.nickname.disabled.1",
            reason="fixture",
            occurred_at="2026-08-23T12:00:00+00:00",
        )


def test_configured_runtime_mutates_and_reads_same_ledger(tmp_path: Path) -> None:
    async def fallback(request: web.Request) -> web.Response:
        return web.json_response({"fallback": request.path})

    async def scenario() -> None:
        root = tmp_path / "data"
        root.mkdir()
        database = root / "private_world" / "private_world.sqlite3"
        ledger = SQLitePrivateWorldLedger(database)
        server = SimpleNamespace(
            handler=fallback,
            letters_adapter=SimpleNamespace(
                memory_prompt_builder=SimpleNamespace(
                    conversation_memory=None,
                    conversation_memory_user_id="local-user",
                )
            ),
            private_world_port=ledger,
            private_world_committer=PrivateWorldDeliveryCommitter(ledger),
            TRUSTED_FRONTEND_ORIGINS=frozenset({TRUSTED_ORIGIN}),
        )
        runtime = create_configured_original_client_server_runtime(
            server_module=server,
            environ={
                "OLIVIA_LOCAL_DATA_ROOT": str(root),
                "OLIVIA_PRIVATE_WORLD_ENABLED": "1",
                "OLIVIA_PRIVATE_WORLD_DB": str(database),
            },
        )
        assert runtime.private_world_commands is not None
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                PRIVATE_WORLD_HOME_ACCESS_PATH,
                json={
                    "home_access": "visit_access",
                    "request_id": "request.home.runtime.1",
                    "reason": "用户明确授权到访。",
                    "occurred_at": "2026-08-23T12:00:00+00:00",
                },
                headers={
                    "Origin": TRUSTED_ORIGIN,
                    CONFIRM_HEADER: CONFIRM_VALUE,
                },
            )
            assert response.status == 200
            read = await client.get(
                "/toy/companion/private-world",
                headers={"Origin": TRUSTED_ORIGIN},
            )
            assert read.status == 200
            assert (await read.json())["home_access"] == "visit_access"
        assert SQLitePrivateWorldLedger(database).snapshot().home_access.value == (
            "visit_access"
        )

    asyncio.run(scenario())
