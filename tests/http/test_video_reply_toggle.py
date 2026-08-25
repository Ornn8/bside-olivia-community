from __future__ import annotations
import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer
from letter_triage import TriageResult
from original_client_companion_mutation_api import CONFIRM_HEADER, CONFIRM_VALUE
from original_client_server import create_configured_original_client_server_runtime
from reply_context import ReplyMode
from reply_orchestrator import ReplyState
from reply_pipeline import PipelineResult
from voice_direction import VoicePerformancePlan
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
def _stub_reply(monkeypatch, *, requested_mode, tmp_path):
    import local_server

    decision = TriageResult(
        "high", requested_mode, "voice_adds_presence", "completed", True,
        direct_response_sufficient=True, voice_materially_better=True,
    )
    observed: dict[str, object] = {}
    async def classify(_content: str) -> TriageResult:
        return decision
    async def run_pipeline(request, context) -> PipelineResult:
        observed["context_mode"] = context.mode
        return PipelineResult("synthetic", ReplyState.COMPLETED, text="canonical reply", quality_status="accepted_degraded")

    async def direct_voice(_reply_text, _gateway, *, request_id=None):
        return VoicePerformancePlan("canonical reply", "warm", 1.05, 0.5, (), (1,))

    def render_video(_reply_text, output_path, **_kwargs):
        observed.setdefault("rendered", []).append(output_path.name)
        output_path.write_bytes(b"synthetic mp4")

    monkeypatch.setattr(local_server.emotion_triage, "classify", classify)
    monkeypatch.setattr(local_server.reply_pipeline, "run", run_pipeline)
    monkeypatch.setattr(local_server, "direct_voice_performance", direct_voice)
    monkeypatch.setattr(local_server, "render_reply_video", render_video)
    scene = tmp_path / "scene.mp4"
    scene.write_bytes(b"synthetic scene")
    for key in ("MORNING", "DAY", "DUSK", "NIGHT"):
        monkeypatch.setenv(f"OLIVIA_SCENE_{key}", str(scene))
    return observed
def _headers() -> dict[str, str]:
    return {"Origin": "http://127.0.0.1:8899", CONFIRM_HEADER: CONFIRM_VALUE}
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
            first = await set_value(False, "video-toggle-replay")
            assert first["status"] == "APPLIED"
            assert await set_value(False, "video-toggle-replay") == first
            conflict = await client.post(
                "/toy/companion/settings/video-reply",
                json={"enabled": True, "request_id": "video-toggle-replay", "reason": "user changed video replies"},
                headers=_headers(),
            )
            assert conflict.status == 409
            assert (await conflict.json())["error_code"] == "VIDEO_REPLY_REQUEST_CONFLICT"
            status = await client.get("/toy/companion/status", headers=read_headers)
            assert (await status.json())["capabilities"]["video_reply"]["enabled"] is False
            assert (await set_value(False, "video-toggle-persist-2"))["status"] == "NOOP"
    asyncio.run(exercise())
    local_server.store.settings.clear()
    local_server._load_store_state()
    assert local_server._video_reply_enabled() is False
@pytest.mark.parametrize(
    ("received_enabled", "later_enabled", "expected_mode"),
    [(False, True, ReplyMode.TEXT_LETTER.value),
     (True, False, ReplyMode.SPOKEN_VIDEO.value)],
)
def test_receive_time_freezes_video_eligibility(monkeypatch, tmp_path, received_enabled, later_enabled, expected_mode) -> None:
    import local_server
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    observed = _stub_reply(
        monkeypatch,
        requested_mode=ReplyMode.SPOKEN_VIDEO.value,
        tmp_path=tmp_path,
    )
    if received_enabled:
        local_server.store.settings[local_server.VIDEO_REPLY_SETTING_KEY] = "legacy"
        assert local_server._video_reply_enabled() is True
    async def exercise() -> dict:
        local_server.video_reply_settings.write_video_reply_enabled(received_enabled)
        sent = await local_server.route(
            "POST", "/toy/letter/send", {"content": "received letter"}, {}, defer_reply=True
        )
        local_server.video_reply_settings.write_video_reply_enabled(later_enabled)
        await asyncio.gather(*tuple(local_server.reply_tasks))
        await asyncio.sleep(0)
        media_tasks = tuple(local_server.media_tasks)
        if media_tasks:
            await asyncio.gather(*media_tasks)
        return sent
    sent = asyncio.run(exercise())
    letter = next(item for item in local_server.store.letters if item["letter_id"] == sent["data"]["letter_id"])
    assert letter["video_reply_enabled_at_receive"] is received_enabled
    assert observed["context_mode"] is ReplyMode(expected_mode)
    assert letter["reply_mode"] == expected_mode
    assert observed.get("rendered", []) == ([] if not received_enabled else [f"{letter['letter_id']}.mp4"])
    assert letter.get("media_status", "NOT_REQUESTED") == ("NOT_REQUESTED" if not received_enabled else "COMPLETED")
