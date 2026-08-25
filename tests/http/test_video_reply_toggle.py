from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from letter_triage import LetterReplyRouter, RoutingContext, TriageResult
from original_client_companion_mutation_api import CONFIRM_HEADER, CONFIRM_VALUE
from original_client_server import create_configured_original_client_server_runtime
from reply_context import ReplyMode
from reply_orchestrator import ReplyState
from reply_pipeline import PipelineResult


def _non_cooperative_renderer(output_path: Path) -> None:
    time.sleep(30)
    Path(output_path).write_bytes(b"must be cancelled")


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
    local_server.media_cancel_events.clear()
    yield
    local_server.store.letters[:] = saved[0]
    local_server.store.request_keys.clear()
    local_server.store.request_keys.update(saved[1])
    local_server.store.settings.clear()
    local_server.store.settings.update(saved[2])
    local_server.media_jobs.clear()
    local_server.media_cancel_events.clear()


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


def test_non_cooperative_renderer_is_terminated_before_publish() -> None:
    import local_server

    output = Path.cwd() / ".video-toggle-cancel-test.mp4"
    output.unlink(missing_ok=True)
    cancellation_event = threading.Event()
    canceller = threading.Timer(0.2, cancellation_event.set)
    canceller.start()

    try:
        assert local_server._run_renderer(
            _non_cooperative_renderer,
            (output,),
            {},
            cancellation_event,
        ) is False
    finally:
        canceller.cancel()
    assert not output.exists()


@pytest.mark.parametrize("requested_mode", [ReplyMode.SPOKEN_VIDEO.value, ReplyMode.MUSICAL_VIDEO.value])
def test_disabling_during_pipeline_downgrades_before_media_queue(monkeypatch, requested_mode) -> None:
    import local_server

    started = asyncio.Event()
    release = asyncio.Event()
    scheduled: list[str] = []
    decision = TriageResult("high", requested_mode, "voice_adds_presence", "completed", True,
                            direct_response_sufficient=False, voice_materially_better=True)

    async def classify(_content: str) -> TriageResult:
        return decision

    async def run_pipeline(_request, _context) -> PipelineResult:
        started.set()
        await release.wait()
        return PipelineResult("synthetic", ReplyState.COMPLETED, text="synthetic reply", quality_status="accepted_degraded")

    monkeypatch.setattr(local_server.emotion_triage, "classify", classify)
    monkeypatch.setattr(local_server.reply_pipeline, "run", run_pipeline)
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(local_server, "_commit_private_world_letter", lambda _letter: False)
    monkeypatch.setattr(local_server, "_schedule_media_job", lambda *args: scheduled.append(args[-1]))
    local_server.video_reply_settings.write_video_reply_enabled(True)
    letter = {"letter_id": "race-letter", "content": "race content", "reply_text": "",
              "reply_mode": ReplyMode.TEXT_LETTER.value, "letter_status": "PENDING"}
    local_server.store.letters[:] = [letter]

    async def exercise() -> None:
        task = asyncio.create_task(local_server.generate_reply(letter["letter_id"], letter["content"]))
        await started.wait()
        local_server.video_reply_settings.write_video_reply_enabled(False)
        release.set()
        await task

    asyncio.run(exercise())
    assert letter["reply_mode"] == ReplyMode.TEXT_LETTER.value
    assert letter.get("media_status", "NOT_REQUESTED") == "NOT_REQUESTED"
    assert scheduled == []


@pytest.mark.parametrize("requested_mode", [ReplyMode.SPOKEN_VIDEO.value, ReplyMode.MUSICAL_VIDEO.value])
def test_queue_boundary_rechecks_video_setting(monkeypatch, requested_mode) -> None:
    import local_server

    enabled_values = iter((True, True, False))
    scheduled: list[str] = []
    decision = TriageResult("high", requested_mode, "voice_adds_presence", "completed", True,
                            direct_response_sufficient=False, voice_materially_better=True)

    async def classify(_content: str) -> TriageResult:
        return decision

    async def run_pipeline(_request, _context) -> PipelineResult:
        return PipelineResult("synthetic", ReplyState.COMPLETED, text="synthetic reply", quality_status="accepted_degraded")

    monkeypatch.setattr(local_server, "_video_reply_enabled", lambda: next(enabled_values, False))
    monkeypatch.setattr(local_server.emotion_triage, "classify", classify)
    monkeypatch.setattr(local_server.reply_pipeline, "run", run_pipeline)
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(local_server, "_commit_private_world_letter", lambda _letter: False)
    monkeypatch.setattr(local_server, "_schedule_media_job", lambda *args: scheduled.append(args[-1]))
    letter = {"letter_id": "queue-race-letter", "content": "queue race", "reply_text": "",
              "reply_mode": ReplyMode.TEXT_LETTER.value, "letter_status": "PENDING"}
    local_server.store.letters[:] = [letter]

    assert asyncio.run(local_server.generate_reply(letter["letter_id"], letter["content"]))
    assert letter["reply_mode"] == ReplyMode.TEXT_LETTER.value
    assert letter["triage"]["reason_code"] == "video_replies_disabled"
    assert letter.get("media_status", "NOT_REQUESTED") == "NOT_REQUESTED"
    assert scheduled == []


@pytest.mark.parametrize("requested_mode", [ReplyMode.SPOKEN_VIDEO.value, ReplyMode.MUSICAL_VIDEO.value])
def test_disabling_running_renderer_requests_cooperative_stop(monkeypatch, tmp_path: Path, requested_mode) -> None:
    import local_server

    scene = tmp_path / "scene.mp4"
    scene.write_bytes(b"scene")
    data_root = tmp_path / "data"
    for key in ("MORNING", "DAY", "DUSK", "NIGHT"):
        monkeypatch.setenv(f"OLIVIA_SCENE_{key}", str(scene))
        monkeypatch.setenv(f"OLIVIA_MUSIC_SCENE_{key}", str(scene))
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(data_root))
    started, release, completed, cooperative = (threading.Event() for _ in range(4))
    local_server.video_reply_settings.write_video_reply_enabled(True)
    local_server.media_semaphore = asyncio.Semaphore(1)
    letter = {"letter_id": f"cancel-{requested_mode}", "content": "letter", "reply_text": "reply",
              "reply_mode": requested_mode, "media_status": "PENDING"}
    local_server.store.letters[:] = [letter]

    async def voice_plan(_letter, text):
        return local_server.VoicePerformancePlan(text, "steady", 1.06, 0.5, (), ())

    def renderer(*args, cancellation_event=None, **_kwargs):
        output = Path(args[2] if requested_mode == ReplyMode.MUSICAL_VIDEO.value else args[1])
        started.set()
        while not release.is_set() and not (cancellation_event and cancellation_event.is_set()):
            time.sleep(0.005)
        if cancellation_event and cancellation_event.is_set():
            letter_id = output.name.removeprefix(".").removesuffix(".render.mp4")
            output.parent.joinpath(f"{letter_id}-official-spoken-v1.mp4").write_bytes(b"partial")
            output.parent.joinpath(f"{letter_id}-song-v2-60s.mp4").write_bytes(b"partial")
            output.parent.joinpath(f".{letter_id}.render-music-v2-60s-stages").mkdir()
            cooperative.set()
        else:
            output.write_bytes(b"must not publish")
        completed.set()

    monkeypatch.setattr(local_server, "_voice_plan_for_letter", voice_plan)
    monkeypatch.setattr(local_server, "_current_music_performance", lambda _env: scene)
    monkeypatch.setattr(local_server, "render_musical_reply", renderer)
    monkeypatch.setattr(local_server, "render_reply_video", renderer)
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)

    async def exercise() -> None:
        task = asyncio.create_task(local_server._render_media_job(
            letter["letter_id"], letter["content"], letter["reply_text"], requested_mode
        ))
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        local_server.video_reply_settings.write_video_reply_enabled(False)
        local_server.video_reply_settings.cancel_video_reply_jobs()
        release.set()
        await task

    asyncio.run(exercise())
    assert completed.is_set()
    assert cooperative.is_set()
    assert letter["media_status"] == "NOT_REQUESTED"
    assert not (data_root / "media" / f"{letter['letter_id']}.mp4").exists()
    assert not (data_root / "media" / f"{letter['letter_id']}-official-spoken-v1.mp4").exists()
    assert not (data_root / "media" / f"{letter['letter_id']}-song-v2-60s.mp4").exists()
    assert not (data_root / "media" / f".{letter['letter_id']}.render-music-v2-60s-stages").exists()
