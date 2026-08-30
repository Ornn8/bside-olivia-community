from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from control_center.auth import CONTROL_CSRF_HEADER
from control_center.runtime import (
    CONTROL_CENTER_RUNTIME_SCHEMA,
    ControlCenterRuntimeError,
    create_configured_control_center_runtime,
    create_control_center_runtime,
)
from private_world_candidates import (
    CandidateStatus,
    CandidateType,
    PrivateWorldCandidate,
    candidate_identity,
)


NOW = datetime(2026, 8, 23, 5, 0, tzinfo=timezone.utc)


def _candidate() -> PrivateWorldCandidate:
    candidate_type = CandidateType.CONFLICT
    return PrivateWorldCandidate(
        candidate_id=candidate_identity(
            "letter-runtime-fixture",
            1,
            candidate_type,
        ),
        source_letter_id="letter-runtime-fixture",
        source_reply_revision=1,
        candidate_type=candidate_type,
        summary="双方对一条边界产生了明确分歧，等待确认。",
        confidence=0.84,
        status=CandidateStatus.PENDING,
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
    )


def _bootstrap_token(url: str) -> str:
    parsed = urlsplit(url)
    values = parse_qs(parsed.fragment)
    return values["bootstrap"][0]


def test_runtime_assembles_shared_ledger_candidates_api_and_ui(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = create_control_center_runtime(
            (tmp_path / "private_world.sqlite3").resolve(),
            candidate_clock=lambda: NOW,
        )
        candidate = _candidate()
        runtime.candidate_store.add(candidate)

        bootstrap_url = runtime.issue_bootstrap_url(port=8900)
        assert bootstrap_url.startswith(
            "http://127.0.0.1:8900/control/#bootstrap="
        )
        assert "?bootstrap=" not in bootstrap_url
        token = _bootstrap_token(bootstrap_url)

        async with TestClient(
            TestServer(runtime.app),
            cookie_jar=CookieJar(unsafe=True),
        ) as client:
            denied = await client.get("/control/candidates")
            assert denied.status == 401

            origin = str(client.make_url("/")).rstrip("/")
            bootstrapped = await client.post(
                "/control/api/session/bootstrap",
                json={"token": token},
                headers={"Origin": origin},
            )
            assert bootstrapped.status == 200
            csrf = (await bootstrapped.json())["data"]["csrf_token"]

            page = await client.get("/control/candidates")
            assert page.status == 200
            assert "待确认建议" in await page.text()

            pending = await client.get(
                "/control/api/private-world/candidates?limit=10"
            )
            assert pending.status == 200
            values = (await pending.json())["data"]["candidates"]
            assert values[0]["candidate_id"] == candidate.candidate_id

            approved = await client.post(
                f"/control/api/private-world/candidates/"
                f"{candidate.candidate_id}/approve",
                json={
                    "request_id": "runtime-review.fixture.1",
                    "reason": "用户在管理界面确认应记录为冲突。",
                    "decided_at": (
                        NOW + timedelta(minutes=5)
                    ).isoformat(),
                },
                headers={
                    "Origin": origin,
                    CONTROL_CSRF_HEADER: csrf,
                },
            )
            assert approved.status == 200
            assert (await approved.json())["data"]["status"] == (
                "approved"
            )

        assert runtime.ledger.snapshot().tension == 3
        assert runtime.candidate_store.get(
            candidate.candidate_id
        ).status is CandidateStatus.APPROVED

    asyncio.run(scenario())


def test_runtime_status_is_sanitized_and_tracks_counts(tmp_path: Path) -> None:
    runtime = create_control_center_runtime(
        (tmp_path / "private_world.sqlite3").resolve()
    )
    runtime.candidate_store.add(_candidate())
    runtime.issue_bootstrap_url()

    status = runtime.public_status()
    assert status["schema_version"] == CONTROL_CENTER_RUNTIME_SCHEMA
    assert status["status"] == "available"
    assert status["network_scope"] == "loopback"
    assert status["candidates"]["pending"] == 1
    assert status["sessions"]["pending_bootstraps"] == 1

    encoded = repr(status)
    assert str(tmp_path) not in encoded
    assert "sqlite3" not in encoded
    assert "relationship" not in encoded
    assert "summary" not in encoded


def test_configured_runtime_uses_default_private_world_path(
    tmp_path: Path,
) -> None:
    runtime = create_configured_control_center_runtime(
        {
            "OLIVIA_LOCAL_DATA_ROOT": str(tmp_path.resolve()),
            "OLIVIA_PRIVATE_WORLD_ENABLED": "1",
        }
    )
    expected = (
        tmp_path / "private_world" / "private_world.sqlite3"
    ).resolve()
    assert expected.is_file()
    assert runtime.ledger.health()["status"] == "READY"


def test_invalid_or_disabled_runtime_fails_with_stable_codes(
    tmp_path: Path,
) -> None:
    try:
        create_control_center_runtime(Path("relative.sqlite3"))
    except ControlCenterRuntimeError as exc:
        assert exc.code == "CONTROL_CENTER_DATABASE_INVALID"
    else:
        raise AssertionError("relative database path must be rejected")

    try:
        create_configured_control_center_runtime(
            {
                "OLIVIA_LOCAL_DATA_ROOT": str(tmp_path.resolve()),
                "OLIVIA_PRIVATE_WORLD_ENABLED": "0",
            }
        )
    except ControlCenterRuntimeError as exc:
        assert exc.code == "PRIVATE_WORLD_DISABLED"
    else:
        raise AssertionError("disabled PrivateWorld must be explicit")

    try:
        create_control_center_runtime(tmp_path.resolve())
    except ControlCenterRuntimeError as exc:
        assert exc.code == "CONTROL_CENTER_DATABASE_INVALID"
    else:
        raise AssertionError("directory paths must be rejected")
