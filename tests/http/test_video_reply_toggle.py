from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from letter_triage import LetterReplyRouter, RoutingContext, TriageResult
from original_client_companion_mutation_api import CONFIRM_HEADER, CONFIRM_VALUE
from original_client_server import create_configured_original_client_server_runtime
from reply_context import ReplyMode
from reply_orchestrator import ReplyState
from reply_pipeline import PipelineResult


@pytest.fixture(autouse=True)
def isolate_local_server_state():
    import local_server

    saved = (
        list(local_server.store.letters),
        dict(local_server.store.request_keys),
        dict(local_server.store.settings),
    )
    local_server.store.letters.clear()
    local_server.store.request_keys.clear()
    local_server.store.settings.clear()
    local_server.media_jobs.clear()
    yield
    local_server.store.letters[:] = saved[0]
    local_server.store.request_keys.clear()
    local_server.store.request_keys.update(saved[1])
    local_server.store.settings.clear()
    local_server.store.settings.update(saved[2])
    local_server.media_jobs.clear()


def _stub_reply(monkeypatch, *, requested_mode, content, reply):
    import local_server

    decision = TriageResult(
        "high", requested_mode, "voice_adds_presence", "completed", True,
        direct_response_sufficient=True, voice_materially_better=True,
    )
    observed: dict[str, object] = {}
    scheduled: list[str] = []

    async def classify(_content: str) -> TriageResult:
        return decision

    async def run_pipeline(request, context) -> PipelineResult:
        observed["request_content"] = request.content
        observed["context_mode"] = context.mode
        return PipelineResult("synthetic", ReplyState.COMPLETED, text=reply, quality_status="accepted_degraded")

    monkeypatch.setattr(local_server.emotion_triage, "classify", classify)
    monkeypatch.setattr(local_server.reply_pipeline, "run", run_pipeline)
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(local_server, "_commit_private_world_letter", lambda _letter: False)
    monkeypatch.setattr(local_server, "_schedule_media_job", lambda _letter_id, _content, _reply, mode: scheduled.append(mode))
    monkeypatch.setattr(local_server.letters_adapter, "remember_conversation", lambda *_args: None)
    return observed, scheduled


def _headers() -> dict[str, str]:
    return {"Origin": "http://127.0.0.1:8899", CONFIRM_HEADER: CONFIRM_VALUE}


def test_disabling_video_replies_keeps_real_send_text_only(monkeypatch, tmp_path) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    observed, scheduled = _stub_reply(
        monkeypatch,
        requested_mode=ReplyMode.SPOKEN_VIDEO.value,
        content="synthetic disabled video request",
        reply="synthetic text-only reply",
    )
    runtime = create_configured_original_client_server_runtime(
        server_module=local_server, environ={"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )

    async def exercise() -> tuple[dict, dict]:
        async with TestClient(TestServer(runtime.app)) as client:
            response = await client.post(
                "/toy/companion/settings/video-reply",
                json={"enabled": False, "request_id": "video-toggle-off-1", "reason": "user disabled video replies"},
                headers=_headers(),
            )
            sent = await local_server.route("POST", "/toy/letter/send", {"content": "synthetic disabled video request"}, {})
            detail = await client.get("/toy/letter/detail", params={"letter_id": sent["data"]["letter_id"]})
            return sent, await detail.json()

    sent, detail = asyncio.run(exercise())
    assert observed["context_mode"] is ReplyMode.TEXT_LETTER
    assert detail["data"]["reply_mode_exact"] == ReplyMode.TEXT_LETTER.value
    assert detail["data"]["reply_text"] == "synthetic text-only reply"
    assert detail["data"].get("media_status", "NOT_REQUESTED") == "NOT_REQUESTED"
    assert scheduled == []


def test_default_and_explicit_enabled_setting_preserve_router_selection(monkeypatch) -> None:
    import local_server

    local_server.store.settings[local_server.VIDEO_REPLY_SETTING_KEY] = "false"
    assert local_server._video_reply_enabled() is True
    local_server.store.settings[local_server.VIDEO_REPLY_SETTING_KEY] = True
    observed, scheduled = _stub_reply(
        monkeypatch,
        requested_mode=ReplyMode.SPOKEN_VIDEO.value,
        content="synthetic default video request",
        reply="synthetic spoken reply",
    )
    letter = {"letter_id": "letter-default-video", "content": "synthetic default video request", "reply_text": "", "reply_mode": ReplyMode.TEXT_LETTER.value, "letter_status": "PENDING"}
    local_server.store.letters[:] = [letter]

    assert asyncio.run(local_server.generate_reply(letter["letter_id"], letter["content"]))
    assert observed["context_mode"] is ReplyMode.SPOKEN_VIDEO
    assert "<ordinary_video_reply_constraints>" in observed["request_content"]
    assert scheduled == [ReplyMode.SPOKEN_VIDEO.value]
    assert letter["reply_mode"] == ReplyMode.SPOKEN_VIDEO.value


def test_video_reply_setting_is_visible_persistent_and_idempotent(monkeypatch, tmp_path) -> None:
    import local_server

    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    runtime = create_configured_original_client_server_runtime(
        server_module=local_server, environ={"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    )

    async def exercise() -> None:
        async with TestClient(TestServer(runtime.app)) as client:
            read_headers = {"Origin": "http://127.0.0.1:8899"}
            status = await client.get("/toy/companion/status", headers=read_headers)
            assert (await status.json())["capabilities"]["video_reply"] == {"enabled": True, "default_enabled": True}

            async def set_value(enabled: bool, request_id: str) -> dict:
                response = await client.post(
                    "/toy/companion/settings/video-reply",
                    json={"enabled": enabled, "request_id": request_id, "reason": "user changed video replies"},
                    headers=_headers(),
                )
                return await response.json()

            assert (await set_value(False, "video-toggle-persist-1"))["status"] == "APPLIED"
            status = await client.get("/toy/companion/status", headers=read_headers)
            assert (await status.json())["capabilities"]["video_reply"]["enabled"] is False
            assert (await set_value(False, "video-toggle-persist-2"))["status"] == "NOOP"

    asyncio.run(exercise())
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    local_server.store.settings.clear()
    local_server._load_store_state()
    assert local_server._video_reply_enabled() is False


def test_router_receives_video_disabled_capabilities() -> None:
    class Gateway:
        async def complete(self, messages, *, request_id=None):
            self.messages = messages
            return type("Response", (), {"text": json.dumps({
                "mode": "text_letter", "reason_code": "direct_words_are_enough", "emotion_level": "normal",
                "music_contexts": [], "music_role": "none", "music_intent": "none", "request_disposition": "none",
                "direct_response_sufficient": True, "voice_materially_better": False, "music_materially_better": False,
                "character_willing": True,
            })})()

    gateway = Gateway()
    result = asyncio.run(LetterReplyRouter(
        gateway,
        routing_context=RoutingContext(True, True),
        video_reply_enabled=lambda: False,
    ).classify("synthetic disabled route"))
    payload = json.loads(gateway.messages[1]["content"])
    assert payload["routing_context"]["spoken_video_available"] is False
    assert payload["routing_context"]["musical_video_available"] is False
    assert result.reply_mode == ReplyMode.TEXT_LETTER.value
