from __future__ import annotations

import asyncio
from importlib import resources
from threading import Lock

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from control_center.app import AUTH_KEY, create_control_app
from control_center.private_world_candidate_api import (
    CandidateDecisionRequest,
    CandidateDecisionResult,
    CandidateSummary,
)
from control_center.private_world_candidate_ui import (
    mount_candidate_control,
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
            self.items.append(event)
            self.current = snapshot
        return True


class RecordingBackend:
    def pending(self, *, limit: int):
        assert limit == 100
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
        return CandidateDecisionResult(
            candidate_id=request.candidate_id,
            decision=request.decision,
            status=(
                "approved"
                if request.decision == "approve"
                else "rejected"
            ),
            reason_code="PRIVATE_WORLD_CANDIDATE_DECISION_RECORDED",
        )


def test_candidate_page_is_protected_and_served_after_bootstrap() -> None:
    async def scenario() -> None:
        app = create_control_app(FakeLedger())
        mount_candidate_control(app, RecordingBackend())
        async with TestClient(
            TestServer(app),
            cookie_jar=CookieJar(unsafe=True),
        ) as client:
            denied = await client.get("/control/candidates")
            assert denied.status == 401
            assert (await denied.json())["error"]["code"] == (
                "CONTROL_SESSION_REQUIRED"
            )

            origin = str(client.make_url("/")).rstrip("/")
            token = app[AUTH_KEY].issue_bootstrap_token()
            session = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            assert session.status == 200

            expected_types = {
                "/control/candidates": {"text/html"},
                "/control/candidates/": {"text/html"},
                "/control/static/candidates.css": {"text/css"},
                "/control/static/candidates.js": {
                    "text/javascript",
                    "application/javascript",
                },
            }
            for path, content_types in expected_types.items():
                response = await client.get(path)
                assert response.status == 200
                assert response.content_type in content_types
                assert response.headers["Cache-Control"] == "no-store"
                assert "Access-Control-Allow-Origin" not in response.headers
                assert await response.text()

            listed = await client.get(
                "/control/api/private-world/candidates?limit=100"
            )
            assert listed.status == 200
            assert (await listed.json())["data"]["candidates"][0][
                "candidate_type"
            ] == "conflict"

    asyncio.run(scenario())


def test_candidate_assets_are_packaged_self_contained_and_non_gamified() -> None:
    root = resources.files("control_center").joinpath("static")
    index = root.joinpath("index.html").read_text(encoding="utf-8")
    page = root.joinpath("candidates.html").read_text(encoding="utf-8")
    css = root.joinpath("candidates.css").read_text(encoding="utf-8")
    script = root.joinpath("candidates.js").read_text(encoding="utf-8")

    assert 'href="/control/candidates"' in index
    assert '<html lang="zh-CN">' in page
    assert 'maxlength="280"' in page
    assert "批准并记录" in page
    assert "拒绝建议" in page
    assert "批准前不会改变任何关系状态" in page
    assert "关系阶段、私人称呼、住所权限和私人世界线不会" in page

    for document in (page, css, script):
        assert "https://" not in document
        assert "http://" not in document
        assert "//cdn" not in document
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script
    assert "sessionStorage" in script
    assert "randomUUID" in script
    assert "getRandomValues" in script
    assert "request_id" in script
    assert "decided_at" in script
    assert "/control/api/private-world/candidates/" in script
    assert "trust =" not in script
    assert "closeness =" not in script
    assert 'type="number"' not in page
