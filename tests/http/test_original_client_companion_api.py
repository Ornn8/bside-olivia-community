from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from original_client_companion_api import (
    TRUSTED_CLIENT_ORIGIN,
    CompanionCandidateSummary,
    CompanionCapability,
    CompanionContinuationSummary,
    CompanionMemorySummary,
    CompanionPrivateWorldSummary,
    CompanionReadStatus,
    mount_original_companion_read_api,
)


class FixtureBackend:
    def __init__(self) -> None:
        self.memory_requests: list[tuple[str | None, int]] = []
        self.candidate_limits: list[int] = []

    def read_status(self) -> CompanionReadStatus:
        return CompanionReadStatus(
            memory=CompanionCapability("available", count=1),
            private_world=CompanionCapability("available"),
            candidates=CompanionCapability("available", count=1),
        )

    def list_memories(self, *, query: str | None, limit: int):
        self.memory_requests.append((query, limit))
        return (
            CompanionMemorySummary(
                memory_id="memory.fixture.1",
                text="用户明确喜欢雨天散步。",
                source_id="reply:fixture:1",
                created_at="2026-08-23T09:00:00+00:00",
                score=0.875,
            ),
        )

    def private_world_summary(self) -> CompanionPrivateWorldSummary:
        return CompanionPrivateWorldSummary(
            version=7,
            relationship_stage="trusted_friend",
            levels={
                "familiarity": "medium",
                "trust": "high",
                "comfort": "medium",
                "closeness": "medium",
                "tension": "low",
            },
            nickname_permissions=("小河豚",),
            home_access="visit_access",
            continuation_facts=(
                CompanionContinuationSummary(
                    fact_id="continuation.yunnan",
                    statement="林离正在准备云南采风。",
                    awareness="character_known",
                ),
            ),
        )

    def list_candidates(self, *, limit: int):
        self.candidate_limits.append(limit)
        return (
            CompanionCandidateSummary(
                candidate_id="candidate.repair.1",
                candidate_type="repair",
                summary="双方已经完成一次明确修复。",
                created_at="2026-08-23T09:05:00+00:00",
                expires_at="2026-08-30T09:05:00+00:00",
            ),
        )


async def _client(backend: object) -> TestClient:
    app = web.Application()
    mount_original_companion_read_api(app, backend)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_original_settings_read_contract_returns_bounded_payloads() -> None:
    async def scenario() -> None:
        backend = FixtureBackend()
        client = await _client(backend)
        headers = {"Origin": TRUSTED_CLIENT_ORIGIN}
        try:
            status = await client.get("/toy/companion/status", headers=headers)
            assert status.status == 200
            status_payload = await status.json()
            assert status_payload["status"] == "READY"
            assert status_payload["capabilities"]["memory"] == {
                "state": "available",
                "count": 1,
            }
            assert status.headers["Access-Control-Allow-Origin"] == TRUSTED_CLIENT_ORIGIN
            assert status.headers["Cache-Control"] == "no-store"

            memory = await client.get(
                "/toy/companion/memory?query=%E9%9B%A8%E5%A4%A9&limit=12",
                headers=headers,
            )
            assert memory.status == 200
            memory_payload = await memory.json()
            assert memory_payload["memories"][0]["memory_id"] == "memory.fixture.1"
            assert memory_payload["memories"][0]["score"] == 0.875
            assert backend.memory_requests == [("雨天", 12)]

            private_world = await client.get(
                "/toy/companion/private-world",
                headers=headers,
            )
            assert private_world.status == 200
            world_payload = await private_world.json()
            assert world_payload["levels"]["trust"] == "high"
            assert world_payload["nickname_permissions"] == ["小河豚"]
            assert world_payload["continuation_facts"][0]["awareness"] == "character_known"
            encoded = json.dumps(world_payload, ensure_ascii=False)
            for hidden in ("database_path", "payload_json", '"trust": 72', '"comfort": 55'):
                assert hidden not in encoded

            candidates = await client.get(
                "/toy/companion/private-world/candidates?limit=8",
                headers=headers,
            )
            assert candidates.status == 200
            candidate_payload = await candidates.json()
            assert candidate_payload["candidates"][0]["candidate_type"] == "repair"
            assert backend.candidate_limits == [8]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_read_contract_requires_original_or_loopback_origin_and_loopback_host() -> None:
    async def scenario() -> None:
        client = await _client(FixtureBackend())
        try:
            missing = await client.get("/toy/companion/status")
            assert missing.status == 403
            assert (await missing.json())["error_code"] == "COMPANION_ORIGIN_FORBIDDEN"

            foreign = await client.get(
                "/toy/companion/status",
                headers={"Origin": "https://example.invalid"},
            )
            assert foreign.status == 403
            assert (await foreign.json())["error_code"] == "COMPANION_ORIGIN_FORBIDDEN"
            assert "Access-Control-Allow-Origin" not in foreign.headers

            invalid_host = await client.get(
                "/toy/companion/status",
                headers={"Origin": TRUSTED_CLIENT_ORIGIN, "Host": "example.invalid"},
            )
            assert invalid_host.status == 403
            assert (await invalid_host.json())["error_code"] == "COMPANION_HOST_FORBIDDEN"

            local = await client.get(
                "/toy/companion/status",
                headers={"Origin": "http://127.0.0.1:3000"},
            )
            assert local.status == 200
            assert local.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:3000"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_query_and_limit_validation_do_not_call_backend() -> None:
    async def scenario() -> None:
        backend = FixtureBackend()
        client = await _client(backend)
        headers = {"Origin": TRUSTED_CLIENT_ORIGIN}
        try:
            for path in (
                "/toy/companion/memory?limit=0",
                "/toy/companion/memory?limit=101",
                "/toy/companion/memory?limit=bad",
                "/toy/companion/private-world/candidates?limit=0",
                "/toy/companion/private-world/candidates?limit=101",
            ):
                response = await client.get(path, headers=headers)
                assert response.status == 400
                assert (await response.json())["error_code"] == "COMPANION_LIMIT_INVALID"

            too_long = await client.get(
                "/toy/companion/memory",
                params={"query": "x" * 501},
                headers=headers,
            )
            assert too_long.status == 400
            assert (await too_long.json())["error_code"] == "COMPANION_QUERY_INVALID"
            assert backend.memory_requests == []
            assert backend.candidate_limits == []
        finally:
            await client.close()

    asyncio.run(scenario())


def test_backend_failures_and_invalid_results_are_sanitized() -> None:
    class FailingBackend(FixtureBackend):
        def read_status(self) -> CompanionReadStatus:
            raise OSError("C:/private/companion.sqlite3")

        def list_memories(self, *, query: str | None, limit: int):
            return ("invalid",)

    async def scenario() -> None:
        client = await _client(FailingBackend())
        headers = {"Origin": TRUSTED_CLIENT_ORIGIN}
        try:
            status = await client.get("/toy/companion/status", headers=headers)
            assert status.status == 503
            payload = await status.json()
            assert payload["error_code"] == "COMPANION_READ_UNAVAILABLE"
            assert "private" not in json.dumps(payload).casefold()

            memory = await client.get("/toy/companion/memory", headers=headers)
            assert memory.status == 503
            assert (await memory.json())["error_code"] == "COMPANION_READ_INVALID"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_mount_requires_typed_backend_and_is_single_use() -> None:
    app = web.Application()
    backend = FixtureBackend()
    mount_original_companion_read_api(app, backend)

    try:
        mount_original_companion_read_api(app, backend)
    except RuntimeError as exc:
        assert str(exc) == "COMPANION_READ_ALREADY_MOUNTED"
    else:
        raise AssertionError("duplicate mount must fail")

    other = web.Application()
    try:
        mount_original_companion_read_api(other, object())  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        raise AssertionError("untyped backend must fail")
