from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from control_center.app import CSRF_HEADER, create_control_app, issue_bootstrap_token
from control_center.private_world_candidate_api import (
    CandidateDecisionRequest,
    CandidateDecisionResult,
    CandidateSummary,
    mount_candidate_review_api,
)


class RecordingCandidateBackend:
    def __init__(self) -> None:
        self.limits: list[int] = []
        self.decisions: list[CandidateDecisionRequest] = []

    def pending(self, *, limit: int):
        self.limits.append(limit)
        return (
            CandidateSummary(
                candidate_id="candidate.conflict.1",
                candidate_type="conflict",
                summary="双方对一个边界产生了明确分歧，等待确认。",
                confidence=0.82,
                source_letter_id="letter-fixture-1",
                source_reply_revision=1,
                created_at="2026-08-23T03:00:00+00:00",
                expires_at="2026-08-30T03:00:00+00:00",
                evidence_refs=("letter:letter-fixture-1", "reply:letter-fixture-1:1"),
            ),
        )

    def decide(self, request: CandidateDecisionRequest) -> CandidateDecisionResult:
        self.decisions.append(request)
        return CandidateDecisionResult(
            candidate_id=request.candidate_id,
            status="approved" if request.decision == "approve" else "rejected",
            decision=request.decision,
            command_id=(
                "command.conflict.1" if request.decision == "approve" else None
            ),
        )


async def _client(backend: RecordingCandidateBackend):
    app = create_control_app()
    mount_candidate_review_api(app, backend)
    token = issue_bootstrap_token(app)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    origin = str(client.make_url("/")).rstrip("/")
    response = await client.post(
        "/control/api/session/bootstrap",
        json={"token": token},
        headers={"Origin": origin},
    )
    assert response.status == 200
    csrf = (await response.json())["csrf_token"]
    return client, origin, csrf


def test_candidate_list_and_approval_require_authenticated_csrf_flow() -> None:
    async def scenario() -> None:
        backend = RecordingCandidateBackend()
        client, origin, csrf = await _client(backend)
        try:
            listed = await client.get("/control/api/private-world/candidates?limit=12")
            assert listed.status == 200
            payload = await listed.json()
            assert payload["candidates"][0]["candidate_type"] == "conflict"
            assert payload["candidates"][0]["confidence"] == 0.82
            assert backend.limits == [12]

            approved = await client.post(
                "/control/api/private-world/candidates/candidate.conflict.1/approve",
                json={
                    "idempotency_key": "candidate-decision.conflict.1",
                    "reason": "用户在管理界面确认应记录为冲突。",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                },
                headers={"Origin": origin, CSRF_HEADER: csrf},
            )
            assert approved.status == 200
            result = await approved.json()
            assert result["status"] == "approved"
            assert result["command_id"] == "command.conflict.1"
            assert len(backend.decisions) == 1
            assert backend.decisions[0].decision == "approve"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_candidate_rejection_records_no_command() -> None:
    async def scenario() -> None:
        backend = RecordingCandidateBackend()
        client, origin, csrf = await _client(backend)
        try:
            rejected = await client.post(
                "/control/api/private-world/candidates/candidate.conflict.1/reject",
                json={
                    "idempotency_key": "candidate-decision.conflict.reject.1",
                    "reason": "用户认为这只是普通讨论，不需要写入关系账本。",
                    "occurred_at": "2026-08-23T03:05:00+00:00",
                },
                headers={"Origin": origin, CSRF_HEADER: csrf},
            )
            assert rejected.status == 200
            payload = await rejected.json()
            assert payload["status"] == "rejected"
            assert "command_id" not in payload
            assert backend.decisions[0].decision == "reject"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_candidate_mutation_rejects_missing_csrf_id_conflict_and_invalid_decision() -> None:
    async def scenario() -> None:
        backend = RecordingCandidateBackend()
        client, origin, csrf = await _client(backend)
        body = {
            "idempotency_key": "candidate-decision.fixture.1",
            "reason": "确认候选。",
            "occurred_at": "2026-08-23T03:05:00+00:00",
        }
        try:
            no_csrf = await client.post(
                "/control/api/private-world/candidates/candidate.conflict.1/approve",
                json=body,
                headers={"Origin": origin},
            )
            assert no_csrf.status == 403
            assert (await no_csrf.json())["error_code"] == "CONTROL_CSRF_INVALID"

            conflicting = dict(body)
            conflicting["candidate_id"] = "candidate.other"
            conflict = await client.post(
                "/control/api/private-world/candidates/candidate.conflict.1/approve",
                json=conflicting,
                headers={"Origin": origin, CSRF_HEADER: csrf},
            )
            assert conflict.status == 400
            assert (await conflict.json())["error_code"] == "PRIVATE_WORLD_CANDIDATE_ID_CONFLICT"

            invalid = await client.post(
                "/control/api/private-world/candidates/candidate.conflict.1/ignore",
                json=body,
                headers={"Origin": origin, CSRF_HEADER: csrf},
            )
            assert invalid.status == 400
            assert (await invalid.json())["error_code"] == "PRIVATE_WORLD_DECISION_INVALID"
            assert backend.decisions == []
        finally:
            await client.close()

    asyncio.run(scenario())


def test_candidate_routes_require_session_and_sanitize_backend_failure() -> None:
    class FailingBackend(RecordingCandidateBackend):
        def pending(self, *, limit: int):
            del limit
            raise OSError("C:/private/path/private_world.sqlite3")

    async def scenario() -> None:
        app = create_control_app()
        backend = FailingBackend()
        mount_candidate_review_api(app, backend)
        async with TestClient(TestServer(app)) as client:
            denied = await client.get("/control/api/private-world/candidates")
            assert denied.status == 403
            assert (await denied.json())["error_code"] == "CONTROL_SESSION_REQUIRED"

        client, _origin, _csrf = await _client(backend)
        try:
            failed = await client.get("/control/api/private-world/candidates")
            assert failed.status == 503
            payload = await failed.json()
            assert payload == {
                "schema_version": "p03.private-world-candidate-control.v1",
                "status": "UNAVAILABLE",
                "error_code": "PRIVATE_WORLD_CANDIDATE_CONTROL_UNAVAILABLE",
            }
            assert "private_world.sqlite3" not in str(payload)
        finally:
            await client.close()

    asyncio.run(scenario())
