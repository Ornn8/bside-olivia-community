from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from threading import Lock

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from control_center.app import AUTH_KEY, create_control_app
from control_center.auth import CONTROL_CSRF_HEADER
from control_center.private_world_candidate_api import (
    CandidateDecisionRequest,
    CandidateDecisionResult,
    CandidateSummary,
    mount_candidate_review_api,
)
from private_world_ledger import LedgerEvent
from private_world_port import PrivateWorldSnapshot


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
        expected_snapshot_version: int | None = None,
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
            ),
        )

    def decide(
        self,
        request: CandidateDecisionRequest,
    ) -> CandidateDecisionResult:
        self.decisions.append(request)
        return CandidateDecisionResult(
            candidate_id=request.candidate_id,
            decision=request.decision,
            status=(
                "approved"
                if request.decision == "approve"
                else "rejected"
            ),
            reason_code=(
                "PRIVATE_WORLD_CANDIDATE_APPROVED"
                if request.decision == "approve"
                else "PRIVATE_WORLD_CANDIDATE_REJECTED"
            ),
        )


async def _authenticated_client(
    backend: RecordingCandidateBackend,
) -> tuple[TestClient, str, str]:
    app = create_control_app(FakeLedger())
    mount_candidate_review_api(app, backend)
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


def test_candidate_list_and_approval_use_shared_auth_boundary() -> None:
    async def scenario() -> None:
        backend = RecordingCandidateBackend()
        client, origin, csrf = await _authenticated_client(backend)
        try:
            listed = await client.get(
                "/control/api/private-world/candidates?limit=12"
            )
            assert listed.status == 200
            payload = (await listed.json())["data"]
            assert payload["schema_version"] == (
                "p03.private-world-candidate-control.v1"
            )
            assert payload["candidates"][0]["candidate_type"] == (
                "conflict"
            )
            assert payload["candidates"][0]["confidence"] == 0.82
            assert backend.limits == [12]

            approved = await client.post(
                "/control/api/private-world/candidates/"
                "candidate.conflict.1/approve",
                json={
                    "request_id": "candidate-decision.conflict.1",
                    "reason": "用户确认应记录为一次冲突。",
                    "decided_at": datetime.now(timezone.utc).isoformat(),
                },
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert approved.status == 200
            result = (await approved.json())["data"]
            assert result["status"] == "approved"
            assert result["reason_code"] == (
                "PRIVATE_WORLD_CANDIDATE_APPROVED"
            )
            assert backend.decisions[0].decision == "approve"
            assert backend.decisions[0].request_id == (
                "candidate-decision.conflict.1"
            )
        finally:
            await client.close()

    asyncio.run(scenario())


def test_candidate_rejection_is_explicit_and_command_free_at_http_layer() -> None:
    async def scenario() -> None:
        backend = RecordingCandidateBackend()
        client, origin, csrf = await _authenticated_client(backend)
        try:
            rejected = await client.post(
                "/control/api/private-world/candidates/"
                "candidate.conflict.1/reject",
                json={
                    "request_id": "candidate-decision.reject.1",
                    "reason": "用户认为这只是普通讨论。",
                    "decided_at": "2026-08-23T03:05:00+00:00",
                },
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert rejected.status == 200
            payload = (await rejected.json())["data"]
            assert payload["status"] == "rejected"
            assert "command_id" not in payload
            assert "command_event_id" not in payload
            assert backend.decisions[0].decision == "reject"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_candidate_routes_require_session_csrf_and_strict_fields() -> None:
    async def scenario() -> None:
        backend = RecordingCandidateBackend()
        app = create_control_app(FakeLedger())
        mount_candidate_review_api(app, backend)
        async with TestClient(
            TestServer(app),
            cookie_jar=CookieJar(unsafe=True),
        ) as unauthenticated_client:
            denied = await unauthenticated_client.get(
                "/control/api/private-world/candidates"
            )
            assert denied.status == 401
            assert (await denied.json())["error"]["code"] == (
                "CONTROL_SESSION_REQUIRED"
            )

        client, origin, csrf = await _authenticated_client(backend)
        body = {
            "request_id": "candidate-decision.fixture.1",
            "reason": "确认候选。",
            "decided_at": "2026-08-23T03:05:00+00:00",
        }
        try:
            no_csrf = await client.post(
                "/control/api/private-world/candidates/"
                "candidate.conflict.1/approve",
                json=body,
                headers={"Origin": origin},
            )
            assert no_csrf.status == 403
            assert (await no_csrf.json())["error"]["code"] == (
                "CONTROL_CSRF_REQUIRED"
            )

            unsupported = await client.post(
                "/control/api/private-world/candidates/"
                "candidate.conflict.1/ignore",
                json=body,
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert unsupported.status == 400
            assert (await unsupported.json())["error"]["code"] == (
                "PRIVATE_WORLD_CANDIDATE_DECISION_INVALID"
            )

            extra_field = await client.post(
                "/control/api/private-world/candidates/"
                "candidate.conflict.1/reject",
                json={**body, "candidate_id": "candidate.other"},
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert extra_field.status == 400
            assert (await extra_field.json())["error"]["code"] == (
                "CONTROL_BODY_FIELDS_INVALID"
            )

            too_long = await client.post(
                "/control/api/private-world/candidates/"
                "candidate.conflict.1/reject",
                json={**body, "reason": "界" * 281},
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert too_long.status == 400
            assert (await too_long.json())["error"]["code"] == (
                "PRIVATE_WORLD_CANDIDATE_DECISION_REASON_INVALID"
            )
            assert backend.decisions == []
        finally:
            await client.close()

    asyncio.run(scenario())


def test_candidate_backend_failures_are_path_free() -> None:
    class FailingBackend(RecordingCandidateBackend):
        def pending(self, *, limit: int):
            del limit
            raise OSError("C:/private/path/private_world.sqlite3")

    async def scenario() -> None:
        backend = FailingBackend()
        client, _origin, _csrf = await _authenticated_client(backend)
        try:
            failed = await client.get(
                "/control/api/private-world/candidates"
            )
            assert failed.status == 503
            payload = await failed.json()
            assert payload == {
                "ok": False,
                "error": {
                    "code": (
                        "PRIVATE_WORLD_CANDIDATE_CONTROL_UNAVAILABLE"
                    )
                },
            }
            assert "private_world.sqlite3" not in str(payload)
        finally:
            await client.close()

    asyncio.run(scenario())
