from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from control_center.app import AUTH_KEY, create_control_app
from private_world_ledger import LedgerEvent
from private_world_port import PrivateWorldSnapshot


NOW = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc).isoformat()


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
                item.event_id == event.event_id
                or item.delivery_id == event.delivery_id
                for item in self.items
            ):
                return False
            self.items.append(event)
            self.current = snapshot
            return True


def _common(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "occurred_at": NOW,
        "reason": "synthetic confirmed change",
        "evidence_refs": ["letter:synthetic-1"],
    }


async def _authenticated_client() -> tuple[TestClient, str]:
    ledger = FakeLedger()
    app = create_control_app(ledger)
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
    payload = await response.json()
    return client, payload["data"]["csrf_token"]


def test_control_boundary_bootstrap_and_security_headers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        del tmp_path
        ledger = FakeLedger()
        app = create_control_app(ledger)
        async with TestClient(
            TestServer(app),
            cookie_jar=CookieJar(unsafe=True),
        ) as client:
            health = await client.get("/control/health")
            assert health.status == 200
            assert (await health.json())["data"] == {
                "status": "READY",
                "authentication": "required",
                "network_scope": "loopback",
            }
            assert health.headers["Cache-Control"] == "no-store"
            assert "default-src 'self'" in health.headers[
                "Content-Security-Policy"
            ]
            assert health.headers["X-Frame-Options"] == "DENY"
            assert "Access-Control-Allow-Origin" not in health.headers

            foreign_get = await client.get(
                "/control/health",
                headers={
                    "Origin": (
                        "https://toy-cnbeta01.olivia.miyoushe.com"
                    )
                },
            )
            assert foreign_get.status == 403
            assert (await foreign_get.json())["error"]["code"] == (
                "CONTROL_ORIGIN_FORBIDDEN"
            )

            forbidden_host = await client.get(
                "/control/health",
                headers={"Host": "example.invalid"},
            )
            assert forbidden_host.status == 403
            assert (
                await forbidden_host.json()
            )["error"]["code"] == "CONTROL_HOST_FORBIDDEN"

            token = app[AUTH_KEY].issue_bootstrap_token()
            foreign = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={
                    "Origin": (
                        "https://toy-cnbeta01.olivia.miyoushe.com"
                    )
                },
            )
            assert foreign.status == 403
            assert (await foreign.json())["error"]["code"] == (
                "CONTROL_ORIGIN_FORBIDDEN"
            )

            origin = str(client.make_url("/")).rstrip("/")
            bootstrap = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            assert bootstrap.status == 200
            body = await bootstrap.json()
            assert body["data"]["csrf_token"]
            cookie = bootstrap.headers["Set-Cookie"]
            assert "HttpOnly" in cookie
            assert "SameSite=Strict" in cookie
            assert "Path=/control" in cookie
            assert "Secure" not in cookie

            reused = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            assert reused.status == 401
            assert (await reused.json())["error"]["code"] == (
                "CONTROL_BOOTSTRAP_INVALID"
            )

            unknown = await client.get("/control/not-found")
            assert unknown.status == 404
            assert (await unknown.json())["error"]["code"] == (
                "CONTROL_ROUTE_NOT_FOUND"
            )
            assert unknown.headers["Referrer-Policy"] == "no-referrer"

    asyncio.run(scenario())


def test_authentication_csrf_and_logout(tmp_path: Path) -> None:
    async def scenario() -> None:
        del tmp_path
        ledger = FakeLedger()
        app = create_control_app(ledger)
        async with TestClient(
            TestServer(app),
            cookie_jar=CookieJar(unsafe=True),
        ) as client:
            unauthenticated = await client.get(
                "/control/api/private-world/snapshot"
            )
            assert unauthenticated.status == 401
            assert (await unauthenticated.json())["error"]["code"] == (
                "CONTROL_SESSION_REQUIRED"
            )

            origin = str(client.make_url("/")).rstrip("/")
            token = app[AUTH_KEY].issue_bootstrap_token()
            bootstrapped = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            csrf = (await bootstrapped.json())["data"]["csrf_token"]

            missing_csrf = await client.post(
                "/control/api/private-world/relationship-events",
                json={
                    **_common("request.missing-csrf"),
                    "event_type": "conflict",
                },
                headers={"Origin": origin},
            )
            assert missing_csrf.status == 403
            assert (await missing_csrf.json())["error"]["code"] == (
                "CONTROL_CSRF_REQUIRED"
            )

            wrong_csrf = await client.post(
                "/control/api/private-world/relationship-events",
                json={
                    **_common("request.wrong-csrf"),
                    "event_type": "conflict",
                },
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": "wrong",
                },
            )
            assert wrong_csrf.status == 403

            logged_out = await client.post(
                "/control/api/session/logout",
                json={},
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                },
            )
            assert logged_out.status == 200
            assert (await logged_out.json())["data"]["status"] == (
                "LOGGED_OUT"
            )

            after = await client.get(
                "/control/api/private-world/snapshot"
            )
            assert after.status == 401

    asyncio.run(scenario())


def test_private_world_mutations_are_typed_idempotent_and_sanitized(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        del tmp_path
        client, csrf = await _authenticated_client()
        try:
            origin = str(client.make_url("/")).rstrip("/")
            headers = {"Origin": origin, "X-CSRF-Token": csrf}

            conflict = await client.post(
                "/control/api/private-world/relationship-events",
                json={
                    **_common("request.conflict-1"),
                    "event_type": "conflict",
                },
                headers=headers,
            )
            assert conflict.status == 200
            conflict_result = (await conflict.json())["data"]["result"]
            assert conflict_result["status"] == "APPLIED"
            assert conflict_result["change_fields"] == ["tension"]

            duplicate = await client.post(
                "/control/api/private-world/relationship-events",
                json={
                    **_common("request.conflict-1"),
                    "event_type": "conflict",
                },
                headers=headers,
            )
            assert duplicate.status == 200
            assert (await duplicate.json())["data"]["result"][
                "status"
            ] == "DUPLICATE"

            snapshot = await client.get(
                "/control/api/private-world/snapshot"
            )
            assert snapshot.status == 200
            state = (await snapshot.json())["data"]
            assert state["version"] == 2
            assert state["levels"] == {
                "familiarity": "unknown",
                "trust": "unknown",
                "comfort": "unknown",
                "closeness": "unknown",
                "tension": "low",
            }
            serialized = json.dumps(state, ensure_ascii=False)
            for raw_score in (
                '"familiarity": 0',
                '"trust": 0',
                '"comfort": 0',
                '"closeness": 0',
                '"tension": 3',
            ):
                assert raw_score not in serialized

            events = await client.get(
                "/control/api/private-world/events"
            )
            timeline = (await events.json())["data"]["events"]
            assert len(timeline) == 1
            event = timeline[0]
            assert event["event_id"] == conflict_result["event_id"]
            assert event["event_type"] == "record_conflict"
            assert event["actor"] == "local_user"
            assert event["source"] == "control_center"
            assert event["reason"] == "synthetic confirmed change"
            assert "command_fingerprint" not in event
            assert "payload_fields" not in event
            assert "delivery_id" not in event
            assert "payload" not in event

            stage = await client.post(
                "/control/api/private-world/relationship-stage",
                json={
                    **_common("request.stage-1"),
                    "target_stage": "familiar",
                    "basis_event_ids": [conflict_result["event_id"]],
                },
                headers=headers,
            )
            assert stage.status == 200
            assert (await stage.json())["data"]["result"]["status"] == (
                "APPLIED"
            )

            nickname = await client.post(
                "/control/api/private-world/nicknames",
                json={
                    **_common("request.nickname-1"),
                    "action": "grant",
                    "nickname": "小河豚",
                },
                headers=headers,
            )
            assert nickname.status == 200

            access = await client.post(
                "/control/api/private-world/home-access",
                json={
                    **_common("request.home-1"),
                    "home_access": "visit_access",
                },
                headers=headers,
            )
            assert access.status == 200

            continuation = await client.post(
                "/control/api/private-world/continuations",
                json={
                    **_common("request.continuation-1"),
                    "action": "upsert",
                    "fact_id": "continuation.synthetic-1",
                    "statement": "一条只用于合成测试的世界线事实。",
                    "awareness": "control_only",
                },
                headers=headers,
            )
            assert continuation.status == 200

            awareness = await client.post(
                "/control/api/private-world/continuations",
                json={
                    **_common("request.continuation-2"),
                    "action": "set_awareness",
                    "fact_id": "continuation.synthetic-1",
                    "awareness": "character_known",
                },
                headers=headers,
            )
            assert awareness.status == 200

            final = await client.get(
                "/control/api/private-world/snapshot"
            )
            final_state = (await final.json())["data"]
            assert final_state["relationship_stage"] == "familiar"
            assert final_state["nickname_permissions"] == ["小河豚"]
            assert final_state["home_access"] == "visit_access"
            assert final_state["continuation_facts"] == [
                {
                    "fact_id": "continuation.synthetic-1",
                    "statement": "一条只用于合成测试的世界线事实。",
                    "awareness": "character_known",
                }
            ]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_api_rejects_client_authority_extra_fields_and_invalid_evidence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        del tmp_path
        client, csrf = await _authenticated_client()
        try:
            origin = str(client.make_url("/")).rstrip("/")
            headers = {"Origin": origin, "X-CSRF-Token": csrf}
            authority = await client.post(
                "/control/api/private-world/relationship-events",
                json={
                    **_common("request.authority"),
                    "event_type": "conflict",
                    "actor": "migration",
                },
                headers=headers,
            )
            assert authority.status == 400
            assert (await authority.json())["error"]["code"] == (
                "CONTROL_BODY_FIELDS_INVALID"
            )

            invalid_event = await client.post(
                "/control/api/private-world/relationship-events",
                json={
                    **_common("request.invalid-event"),
                    "event_type": "confession",
                },
                headers=headers,
            )
            assert invalid_event.status == 400

            missing_basis = await client.post(
                "/control/api/private-world/relationship-stage",
                json={
                    **_common("request.missing-basis"),
                    "target_stage": "close",
                    "basis_event_ids": ["event.missing"],
                },
                headers=headers,
            )
            assert missing_basis.status == 400
            assert (await missing_basis.json())["error"]["code"] == (
                "PRIVATE_WORLD_COMMAND_EVIDENCE_INVALID"
            )

            invalid_content_type = await client.post(
                "/control/api/private-world/nicknames",
                data="{}",
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                    "Content-Type": "text/plain",
                },
            )
            assert invalid_content_type.status == 415
            assert (await invalid_content_type.json())["error"]["code"] == (
                "CONTROL_CONTENT_TYPE_INVALID"
            )
        finally:
            await client.close()

    asyncio.run(scenario())
