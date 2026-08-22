from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
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
                row.event_id == event.event_id
                or row.delivery_id == event.delivery_id
                for row in self.items
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


async def _start_authenticated() -> tuple[TestClient, str, str]:
    app = create_control_app(FakeLedger())
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


def test_boundary_bootstrap_and_security_headers() -> None:
    async def scenario() -> None:
        app = create_control_app(FakeLedger())
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

            for headers, code in (
                ({"Host": "example.invalid"}, "CONTROL_HOST_FORBIDDEN"),
                (
                    {
                        "Origin": (
                            "https://toy-cnbeta01.olivia.miyoushe.com"
                        )
                    },
                    "CONTROL_ORIGIN_FORBIDDEN",
                ),
            ):
                denied = await client.get("/control/health", headers=headers)
                assert denied.status == 403
                assert (await denied.json())["error"]["code"] == code

            origin = str(client.make_url("/")).rstrip("/")
            token = app[AUTH_KEY].issue_bootstrap_token()
            bootstrapped = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            assert bootstrapped.status == 200
            cookie = bootstrapped.headers["Set-Cookie"]
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


def test_authentication_csrf_and_logout() -> None:
    async def scenario() -> None:
        app = create_control_app(FakeLedger())
        async with TestClient(
            TestServer(app),
            cookie_jar=CookieJar(unsafe=True),
        ) as client:
            unauthenticated = await client.get(
                "/control/api/private-world/snapshot"
            )
            assert unauthenticated.status == 401

            origin = str(client.make_url("/")).rstrip("/")
            token = app[AUTH_KEY].issue_bootstrap_token()
            session = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            csrf = (await session.json())["data"]["csrf_token"]
            body = {
                **_common("request.csrf"),
                "event_type": "conflict",
            }

            missing = await client.post(
                "/control/api/private-world/relationship-events",
                json=body,
                headers={"Origin": origin},
            )
            assert missing.status == 403
            assert (await missing.json())["error"]["code"] == (
                "CONTROL_CSRF_REQUIRED"
            )

            wrong = await client.post(
                "/control/api/private-world/relationship-events",
                json=body,
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": "wrong",
                },
            )
            assert wrong.status == 403

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
            assert (
                await client.get(
                    "/control/api/private-world/snapshot"
                )
            ).status == 401

    asyncio.run(scenario())


def test_private_world_mutations_are_typed_and_sanitized() -> None:
    async def scenario() -> None:
        client, origin, csrf = await _start_authenticated()
        headers = {"Origin": origin, "X-CSRF-Token": csrf}
        try:
            conflict = await client.post(
                "/control/api/private-world/relationship-events",
                json={
                    **_common("request.conflict-1"),
                    "event_type": "conflict",
                },
                headers=headers,
            )
            result = (await conflict.json())["data"]["result"]
            assert result["status"] == "APPLIED"
            assert result["change_fields"] == ["tension"]

            duplicate = await client.post(
                "/control/api/private-world/relationship-events",
                json={
                    **_common("request.conflict-1"),
                    "event_type": "conflict",
                },
                headers=headers,
            )
            assert (await duplicate.json())["data"]["result"]["status"] == (
                "DUPLICATE"
            )

            snapshot = await client.get(
                "/control/api/private-world/snapshot"
            )
            state = (await snapshot.json())["data"]
            assert state["levels"] == {
                "familiarity": "unknown",
                "trust": "unknown",
                "comfort": "unknown",
                "closeness": "unknown",
                "tension": "low",
            }
            serialized = json.dumps(state, ensure_ascii=False)
            assert '"tension": 3' not in serialized
            assert '"trust": 0' not in serialized

            events_response = await client.get(
                "/control/api/private-world/events"
            )
            timeline = (await events_response.json())["data"]["events"]
            event = timeline[0]
            assert event["event_type"] == "record_conflict"
            assert event["actor"] == "local_user"
            assert event["source"] == "control_center"
            for hidden in (
                "command_fingerprint",
                "payload_fields",
                "delivery_id",
                "payload",
            ):
                assert hidden not in event

            requests = (
                (
                    "/control/api/private-world/relationship-stage",
                    {
                        **_common("request.stage-1"),
                        "target_stage": "familiar",
                        "basis_event_ids": [result["event_id"]],
                    },
                ),
                (
                    "/control/api/private-world/nicknames",
                    {
                        **_common("request.nickname-1"),
                        "action": "grant",
                        "nickname": "小河豚",
                    },
                ),
                (
                    "/control/api/private-world/home-access",
                    {
                        **_common("request.home-1"),
                        "home_access": "visit_access",
                    },
                ),
                (
                    "/control/api/private-world/continuations",
                    {
                        **_common("request.continuation-1"),
                        "action": "upsert",
                        "fact_id": "continuation.synthetic-1",
                        "statement": "一条只用于合成测试的世界线事实。",
                        "awareness": "control_only",
                    },
                ),
                (
                    "/control/api/private-world/continuations",
                    {
                        **_common("request.continuation-2"),
                        "action": "set_awareness",
                        "fact_id": "continuation.synthetic-1",
                        "awareness": "character_known",
                    },
                ),
            )
            for path, body in requests:
                response = await client.post(
                    path,
                    json=body,
                    headers=headers,
                )
                assert response.status == 200

            final_response = await client.get(
                "/control/api/private-world/snapshot"
            )
            final = (await final_response.json())["data"]
            assert final["relationship_stage"] == "familiar"
            assert final["nickname_permissions"] == ["小河豚"]
            assert final["home_access"] == "visit_access"
            assert final["continuation_facts"] == [
                {
                    "fact_id": "continuation.synthetic-1",
                    "statement": "一条只用于合成测试的世界线事实。",
                    "awareness": "character_known",
                }
            ]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_api_rejects_client_authority_and_invalid_inputs() -> None:
    async def scenario() -> None:
        client, origin, csrf = await _start_authenticated()
        headers = {"Origin": origin, "X-CSRF-Token": csrf}
        try:
            cases = (
                (
                    "/control/api/private-world/relationship-events",
                    {
                        **_common("request.authority"),
                        "event_type": "conflict",
                        "actor": "migration",
                    },
                    "CONTROL_BODY_FIELDS_INVALID",
                ),
                (
                    "/control/api/private-world/relationship-events",
                    {
                        **_common("request.invalid-event"),
                        "event_type": "confession",
                    },
                    "CONTROL_RELATIONSHIP_EVENT_INVALID",
                ),
                (
                    "/control/api/private-world/relationship-stage",
                    {
                        **_common("request.missing-basis"),
                        "target_stage": "close",
                        "basis_event_ids": ["event.missing"],
                    },
                    "PRIVATE_WORLD_COMMAND_EVIDENCE_INVALID",
                ),
            )
            for path, body, code in cases:
                response = await client.post(
                    path,
                    json=body,
                    headers=headers,
                )
                assert response.status == 400
                assert (await response.json())["error"]["code"] == code

            invalid_content_type = await client.post(
                "/control/api/private-world/nicknames",
                data="{}",
                headers={
                    **headers,
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
