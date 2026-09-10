import asyncio
from collections import deque
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from runtime.diagnostics.support_bundle import _project_tail
from runtime.video_reply_settings import VideoReplySettingsStore


@pytest.mark.parametrize("reason,expected", [
    ("router_quota_exhausted", "LLM_QUOTA_EXHAUSTED"),
    ("router_auth_failed", "LLM_AUTH_FAILED"),
    ("router_timeout", "LLM_TIMEOUT"),
    ("router_rate_limited", "LLM_RATE_LIMITED"),
    ("router_invalid_result", "REPLY_ROUTE_INVALID_RESULT"),
    ("private-key-and-content", "VIDEO_TRIAGE_UNAVAILABLE"),
])
def test_preview_failure_reaches_export_without_private_input(tmp_path, monkeypatch, reason, expected):
    import local_server as server

    monkeypatch.setattr(server, "video_reply_settings_store", VideoReplySettingsStore.initialize(tmp_path))
    monkeypatch.setattr(server, "_RUNTIME_DIAGNOSTIC_EVENTS", deque(maxlen=200))

    async def classify(*args):
        return SimpleNamespace(status="unavailable", reason_code=reason)

    monkeypatch.setattr(server, "_classify_managed_route", classify)

    async def run():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", server.handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/toy/letter/route-preview", json={"content": "private-letter-text"})
            payload = await response.json()
            assert payload["data"]["error_code"] == expected

    asyncio.run(run())
    records = server.runtime_diagnostic_event_snapshot()
    for event in ("reply_route_classification_failed", "reply_route_preview_result"):
        assert {"event": event, "status": "FAILED", "error_code": expected} in records
    exported = _project_tail(records, runtime=True)
    assert expected.encode() in exported
    assert b"private-" not in exported
