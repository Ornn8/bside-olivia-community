from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from original_client_companion_mutation_api import (
    CANDIDATE_DECISION_PATH,
    CONFIRM_HEADER,
    CONFIRM_VALUE,
    CompanionMutationResult,
    MEMORY_CORRECT_PATH,
    MEMORY_DELETE_PATH,
    OriginalClientCompanionMutationError,
    mount_original_client_companion_mutation_api,
)


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def correct_memory(self, **kwargs) -> CompanionMutationResult:
        self.calls.append(("correct_memory", kwargs))
        return CompanionMutationResult(
            request_id=str(kwargs["request_id"]),
            status="APPLIED",
            affected_count=2,
        )

    def delete_memory(self, **kwargs) -> CompanionMutationResult:
        self.calls.append(("delete_memory", kwargs))
        return CompanionMutationResult(
            request_id=str(kwargs["request_id"]),
            status="APPLIED",
            affected_count=1,
        )

    def decide_candidate(self, **kwargs) -> CompanionMutationResult:
        self.calls.append(("decide_candidate", kwargs))
        return CompanionMutationResult(
            request_id=str(kwargs["request_id"]),
            status="APPLIED",
            affected_count=1,
        )


async def _client(backend: RecordingBackend | None = None):
    from aiohttp import web

    app = web.Application()
    backend = backend or RecordingBackend()
    mount_original_client_companion_mutation_api(app, backend)
    client = TestClient(TestServer(app))
    await client.start_server()
    origin = str(client.make_url("/")).rstrip("/")
    return client, backend, origin


def _headers(origin: str) -> dict[str, str]:
    return {
        "Origin": origin,
        CONFIRM_HEADER: CONFIRM_VALUE,
        "Content-Type": "application/json",
    }


def test_memory_correction_and_delete_delegate_exact_confirmed_requests() -> None:
    async def scenario() -> None:
        client, backend, origin = await _client()
        try:
            corrected = await client.post(
                MEMORY_CORRECT_PATH,
                json={
                    "memory_id": "memory.fixture.1",
                    "replacement_text": "用户现在住在东京北区。",
                    "request_id": "request.memory.correct.1",
                    "reason": "用户明确纠正了居住信息。",
                },
                headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert corrected.status == 200
            assert (await corrected.json())["affected_count"] == 2

            deleted = await client.post(
                MEMORY_DELETE_PATH,
                json={
                    "memory_id": "memory.fixture.2",
                    "request_id": "request.memory.delete.1",
                    "reason": "用户确认该事实错误。",
                },
                headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert deleted.status == 200
            assert (await deleted.json())["affected_count"] == 1

            assert backend.calls == [
                (
                    "correct_memory",
                    {
                        "memory_id": "memory.fixture.1",
                        "replacement_text": "用户现在住在东京北区。",
                        "request_id": "request.memory.correct.1",
                        "reason": "用户明确纠正了居住信息。",
                    },
                ),
                (
                    "delete_memory",
                    {
                        "memory_id": "memory.fixture.2",
                        "request_id": "request.memory.delete.1",
                        "reason": "用户确认该事实错误。",
                    },
                ),
            ]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_candidate_approve_and_reject_preserve_decision_envelope() -> None:
    async def scenario() -> None:
        client, backend, origin = await _client()
        decided_at = datetime.now(timezone.utc).isoformat()
        try:
            for decision in ("approve", "reject"):
                response = await client.post(
                    f"/toy/companion/private-world/candidates/candidate.fixture.1/{decision}",
                    json={
                        "request_id": f"request.candidate.{decision}.1",
                        "reason": "用户在原版设置中明确确认。",
                        "decided_at": decided_at,
                    },
                    headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
                )
                assert response.status == 200
                assert (await response.json())["status"] == "APPLIED"

            assert backend.calls[0][1]["decision"] == "approve"
            assert backend.calls[1][1]["decision"] == "reject"
            assert backend.calls[0][1]["decided_at"] == decided_at
        finally:
            await client.close()

    asyncio.run(scenario())


def test_preflight_is_origin_bounded_and_exposes_only_required_headers() -> None:
    async def scenario() -> None:
        client, _backend, origin = await _client()
        try:
            allowed = await client.options(
                MEMORY_CORRECT_PATH,
                headers={"Origin": origin},
            )
            assert allowed.status == 204
            assert allowed.headers["Access-Control-Allow-Origin"] == origin
            assert allowed.headers["Access-Control-Allow-Methods"] == "POST, OPTIONS"
            assert CONFIRM_HEADER in allowed.headers["Access-Control-Allow-Headers"]

            foreign = await client.options(
                MEMORY_CORRECT_PATH,
                headers={"Origin": "https://example.invalid"},
            )
            assert foreign.status == 403
            assert "Access-Control-Allow-Origin" not in foreign.headers
        finally:
            await client.close()

    asyncio.run(scenario())


def test_mutations_require_origin_confirmation_json_and_exact_fields() -> None:
    async def scenario() -> None:
        client, backend, origin = await _client()
        body = {
            "memory_id": "memory.fixture.1",
            "request_id": "request.memory.delete.1",
            "reason": "用户确认删除。",
        }
        try:
            missing_origin = await client.post(
                MEMORY_DELETE_PATH,
                json=body,
                headers={CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert missing_origin.status == 403

            missing_confirmation = await client.post(
                MEMORY_DELETE_PATH,
                json=body,
                headers={"Origin": origin},
            )
            assert missing_confirmation.status == 403
            assert (await missing_confirmation.json())["error_code"] == "COMPANION_CONFIRMATION_REQUIRED"

            wrong_type = await client.post(
                MEMORY_DELETE_PATH,
                data="fixture",
                headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert wrong_type.status == 415

            extra = dict(body)
            extra["hidden_score"] = 100
            extra_field = await client.post(
                MEMORY_DELETE_PATH,
                json=extra,
                headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert extra_field.status == 400
            assert (await extra_field.json())["error_code"] == "COMPANION_FIELDS_INVALID"
            assert backend.calls == []
        finally:
            await client.close()

    asyncio.run(scenario())


def test_invalid_decision_time_and_path_are_rejected_before_backend() -> None:
    async def scenario() -> None:
        client, backend, origin = await _client()
        try:
            bad_time = await client.post(
                "/toy/companion/private-world/candidates/candidate.fixture.1/approve",
                json={
                    "request_id": "request.candidate.approve.1",
                    "reason": "用户确认。",
                    "decided_at": "2026-08-23T12:00:00",
                },
                headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert bad_time.status == 400
            assert (await bad_time.json())["error_code"] == "COMPANION_DECISION_TIME_INVALID"

            bad_decision = await client.post(
                "/toy/companion/private-world/candidates/candidate.fixture.1/auto",
                json={
                    "request_id": "request.candidate.auto.1",
                    "reason": "fixture",
                    "decided_at": "2026-08-23T12:00:00+00:00",
                },
                headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert bad_decision.status == 404
            assert backend.calls == []
        finally:
            await client.close()

    asyncio.run(scenario())


def test_backend_failure_is_path_free() -> None:
    class FailingBackend(RecordingBackend):
        def delete_memory(self, **kwargs) -> CompanionMutationResult:
            raise OSError("C:/private/qdrant/data")

    async def scenario() -> None:
        client, _backend, origin = await _client(FailingBackend())
        try:
            response = await client.post(
                MEMORY_DELETE_PATH,
                json={
                    "memory_id": "memory.fixture.1",
                    "request_id": "request.memory.delete.1",
                    "reason": "用户确认删除。",
                },
                headers={"Origin": origin, CONFIRM_HEADER: CONFIRM_VALUE},
            )
            assert response.status == 503
            payload = await response.json()
            assert payload["error_code"] == "COMPANION_MUTATION_UNAVAILABLE"
            assert "qdrant" not in str(payload).casefold()
            assert "private" not in str(payload).casefold()
        finally:
            await client.close()

    asyncio.run(scenario())
