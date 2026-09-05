from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import local_server
from letter_triage import TriageResult
from llm_gateway import GatewayConfig, GatewayRequestScope
from runtime.reply.reply_context import ReplyMode
from reply_orchestrator import ReplyState
from runtime.reply.reply_pipeline import PipelineResult
from runtime.reply.reply_quality_gate import DeliveryRepairDisposition
from voice_direction import VoiceDirectionError, VoicePerformancePlan


def test_text_delay_records_deadline_without_blocking(monkeypatch):
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_ENABLED", "1")
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_MINUTES_MIN", "5")
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_MINUTES_MAX", "10")
    monkeypatch.setattr(
        local_server.random,
        "uniform",
        lambda _minimum, _maximum: 5.0,
    )
    letter = {}
    local_server._schedule_text_reply_delay(letter, "text_letter")
    assert letter["reply_delay_minutes"] == 5.0
    assert letter["reply_not_before"] > 0


def test_both_video_modes_skip_letter_delay_and_render_off_loop(monkeypatch):
    monkeypatch.setenv("OLIVIA_REPLY_DELAY_ENABLED", "1")
    for mode in ("spoken_video", "musical_video"):
        letter = {}
        local_server._schedule_text_reply_delay(letter, mode)
        assert letter["reply_delay_minutes"] == 0.0
        assert letter["reply_not_before"] == 0.0
    assert asyncio.iscoroutinefunction(local_server._render_media_job)


def test_exact_modes_keep_legacy_wire_compatibility():
    assert local_server._wire_reply_mode("text_letter") == "text"
    assert local_server._wire_reply_mode("spoken_video") == "video"
    assert local_server._wire_reply_mode("musical_video") == "video"
    assert local_server._exact_reply_mode("video") == "musical_video"
    assert local_server._exact_reply_mode("spoken_video") == "musical_video"


def test_media_output_directory_failure_is_normalized(tmp_path: Path, monkeypatch):
    blocked_root = tmp_path / "blocked"
    blocked_root.write_text("not a directory", encoding="utf-8")
    letter = {"letter_id": "mkdir-failure", "media_status": "PENDING"}
    local_server.store.letters[:] = [letter]
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(blocked_root))
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)

    asyncio.run(local_server._render_media_job(
        "mkdir-failure", "letter", "reply", ReplyMode.SPOKEN_VIDEO.value
    ))

    assert (letter["media_status"], letter["media_error_code"], letter["media_retryable"]) == (
        "UNAVAILABLE", "MEDIA_PROVIDER_UNAVAILABLE", True
    )


def test_generation_rechecks_breeze_gpu_before_invoking_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    letter = {"letter_id": "gpu-preflight", "media_status": "PENDING"}
    local_server.store.letters[:] = [letter]
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)
    monkeypatch.setattr(
        local_server,
        "require_breeze_hardware",
        lambda: (_ for _ in ()).throw(
            local_server.MusicReplyError("BREEZE_TTS_10GB_VRAM_REQUIRED")
        ),
    )
    monkeypatch.setattr(
        local_server,
        "render_reply_video",
        lambda *_args, **_kwargs: pytest.fail("renderer must not run"),
    )

    asyncio.run(
        local_server._render_media_job(
            "gpu-preflight", "letter", "reply", ReplyMode.SPOKEN_VIDEO.value
        )
    )

    assert letter["media_status"] == "UNAVAILABLE"
    assert letter["media_error_code"] == "BREEZE_TTS_10GB_VRAM_REQUIRED"


def test_successful_media_retry_clears_the_previous_failure_code(
    tmp_path: Path,
    monkeypatch,
):
    scene = tmp_path / "scene.mp4"
    scene.write_bytes(b"scene")
    official_reference = tmp_path / "official-complete-reply.mp4"
    official_reference.write_bytes(b"official")
    letter = {
        "letter_id": "retry-media",
        "content": "letter",
        "reply_text": "reply",
        "reply_mode": "musical_video",
        "media_status": "UNAVAILABLE",
        "media_error_code": "MINIMAX_MUSIC3_FAILED",
        "music_duration_seconds": 60,
    }
    local_server.store.letters[:] = [letter]
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("OLIVIA_MUSIC_PERFORMANCE_BASE", str(scene))
    monkeypatch.setenv("OLIVIA_OFFICIAL_REPLY_REFERENCE", str(official_reference))
    monkeypatch.setenv("OLIVIA_SPOKEN_SCENE_CANDIDATES", str(scene))
    monkeypatch.setenv("OLIVIA_ORDINARY_ACTION_BASE", str(scene))
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)
    observed = {}

    async def voice_plan(_letter, text):
        return VoicePerformancePlan(
            reply_text=text,
            overall_emotion="steady",
            global_speed=1.06,
            energy=0.5,
            breath_before_sentences=(),
            emphasize_sentences=(),
            short_instruction="",
            profile="legacy_music_global_direction_v1",
        )

    def render(_content, _reply, output, **kwargs):
        observed.update(kwargs)
        Path(output).write_bytes(b"final-video")
        return {}

    monkeypatch.setattr(local_server, "render_musical_reply", render)
    monkeypatch.setattr(local_server, "_music_voice_plan_for_letter", voice_plan)

    asyncio.run(
        local_server._render_media_job(
            "retry-media", "letter", "reply", "musical_video"
        )
    )

    assert letter["media_status"] == "COMPLETED"
    assert letter["media_error_code"] is None
    assert observed["official_reply_reference_path"] == official_reference
    assert observed["spoken_action_base_path"] == scene
    assert observed["performance_video_path"] == scene
    assert observed["gateway"] is local_server.letters_adapter.gateway
    assert "normal_scene_path" not in observed
    assert observed["normal_video_path"].name.endswith("-official-spoken-v1.mp4")
    assert observed["song_video_path"].name.endswith("-song-v2-60s.mp4")


def test_spoken_media_resolves_relative_renderer_paths_from_project_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "app"
    scene = project_root / "assets" / "scene.mp4"
    tts_config = project_root / "config" / "tts.json"
    latentsync_python = project_root / "providers" / "latentsync" / "python.exe"
    latentsync_root = project_root / "providers" / "latentsync"
    for path in (scene, tts_config, latentsync_python):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    data_root = tmp_path / "data"
    letter = {"letter_id": "relative-media", "media_status": "PENDING"}
    local_server.store.letters[:] = [letter]
    monkeypatch.setenv("OLIVIA_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(data_root))
    monkeypatch.setenv("OLIVIA_TTS_CONFIG", "config/tts.json")
    monkeypatch.setenv("OLIVIA_LATENTSYNC_PYTHON", "providers/latentsync/python.exe")
    monkeypatch.setenv("OLIVIA_LATENTSYNC_ROOT", "providers/latentsync")
    monkeypatch.setenv("OLIVIA_ORDINARY_ACTION_BASE", "assets/scene.mp4")
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)

    async def voice_plan(_letter, text):
        return VoicePerformancePlan(
            reply_text=text,
            overall_emotion="steady",
            global_speed=1.0,
            energy=0.5,
            breath_before_sentences=(),
            emphasize_sentences=(),
        )

    observed: dict[str, Path] = {}

    def render(_reply, output, **kwargs):
        observed.update(kwargs)
        Path(output).write_bytes(b"video")

    monkeypatch.setattr(local_server, "_voice_plan_for_letter", voice_plan)
    monkeypatch.setattr(local_server, "render_reply_video", render, raising=False)
    monkeypatch.setattr(local_server, "render_musical_reply", lambda *_a, **_k: pytest.fail("music renderer called"))

    asyncio.run(
        local_server._render_media_job(
            "relative-media", "letter", "reply", ReplyMode.SPOKEN_VIDEO.value
        )
    )

    assert letter["media_status"] == "COMPLETED"
    assert observed["scene_path"] == scene
    assert observed["tts_config_path"] == tts_config
    assert observed["latentsync_python_path"] == latentsync_python
    assert observed["latentsync_root"] == latentsync_root
    assert observed["environment"]["OLIVIA_LATENTSYNC_PYTHON"] == (
        "providers/latentsync/python.exe"
    )
    assert observed["environment"]["OLIVIA_LATENTSYNC_ROOT"] == (
        "providers/latentsync"
    )


def test_spoken_media_keeps_its_entry_environment_across_voice_plan_await(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "app"
    scene = project_root / "assets" / "scene.mp4"
    tts_config = project_root / "config" / "tts.json"
    visual_config = project_root / "config" / "visual.json"
    worker = project_root / "workers" / "visual.py"
    latentsync_python = project_root / "providers" / "latentsync" / "python.exe"
    latentsync_root = project_root / "providers" / "latentsync"
    for path in (scene, tts_config, visual_config, worker, latentsync_python):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    environment = {
        "OLIVIA_PROJECT_ROOT": str(project_root),
        "OLIVIA_TTS_CONFIG": "config/tts.json",
        "OLIVIA_VISUAL_CONFIG": "config/visual.json",
        "OLIVIA_LIVETALKING_WORKER": "workers/visual.py",
        "OLIVIA_LATENTSYNC_PYTHON": "providers/latentsync/python.exe",
        "OLIVIA_LATENTSYNC_ROOT": "providers/latentsync",
        "OLIVIA_ORDINARY_ACTION_BASE": "assets/scene.mp4",
        "OLIVIA_OFFICIAL_REPLY_REFERENCE": "assets/scene.mp4",
        "OLIVIA_MUSIC_PERFORMANCE_BASE": "assets/scene.mp4",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    letter = {"letter_id": "immutable-spoken-media", "media_status": "PENDING"}
    local_server.store.letters[:] = [letter]
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)

    async def voice_plan(_letter, text):
        for name in environment:
            monkeypatch.delenv(name, raising=False)
        return VoicePerformancePlan(
            reply_text=text,
            overall_emotion="steady",
            global_speed=1.0,
            energy=0.5,
            breath_before_sentences=(),
            emphasize_sentences=(),
        )

    observed: dict[str, object] = {}

    def render(_reply, output, **kwargs):
        observed.update(kwargs)
        Path(output).write_bytes(b"video")

    monkeypatch.setattr(local_server, "_voice_plan_for_letter", voice_plan)
    monkeypatch.setattr(local_server, "render_reply_video", render)

    asyncio.run(
        local_server._render_media_job(
            "immutable-spoken-media", "letter", "reply", ReplyMode.SPOKEN_VIDEO.value
        )
    )

    assert letter["media_status"] == "COMPLETED"
    assert observed["tts_config_path"] == tts_config
    assert observed["scene_path"] == scene
    assert observed["latentsync_python_path"] == latentsync_python
    assert observed["latentsync_root"] == latentsync_root
    assert observed["environment"]["OLIVIA_PROJECT_ROOT"] == str(project_root)


@pytest.mark.parametrize(
    "root_alias",
    ("anchor/../data", "data-junction"),
    ids=("dotdot", "directory-junction"),
)
def test_local_data_root_alias_drives_state_and_serves_rendered_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_alias: str
) -> None:
    project_root = tmp_path / "app"
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    data_root = project_root / "data"
    if root_alias == "data-junction":
        data_root.mkdir(parents=True)
        junction = project_root / root_alias
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(data_root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"directory junctions are unavailable: {result.stderr}")
    scene = project_root / "assets" / "scene.mp4"
    scene.parent.mkdir(parents=True)
    scene.write_bytes(b"scene")
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setenv("OLIVIA_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", root_alias)
    monkeypatch.setenv("OLIVIA_ORDINARY_ACTION_BASE", "assets/scene.mp4")
    monkeypatch.setenv("OLIVIA_OFFICIAL_REPLY_REFERENCE", "assets/scene.mp4")
    monkeypatch.setenv("OLIVIA_MUSIC_PERFORMANCE_BASE", "assets/scene.mp4")

    letter = {
        "letter_id": "relative-data-root",
        "content": "letter",
        "reply_text": "reply",
        "reply_mode": ReplyMode.SPOKEN_VIDEO.value,
        "letter_status": "COMPLETED",
        "media_status": "PENDING",
        "reply_not_before": 0.0,
    }
    local_server.store.letters[:] = [letter]

    async def voice_plan(_letter, text):
        return VoicePerformancePlan(
            reply_text=text,
            overall_emotion="steady",
            global_speed=1.0,
            energy=0.5,
            breath_before_sentences=(),
            emphasize_sentences=(),
        )

    def render(_reply, output, **_kwargs):
        Path(output).write_bytes(b"video")

    monkeypatch.setattr(local_server, "_voice_plan_for_letter", voice_plan)
    monkeypatch.setattr(local_server, "render_reply_video", render)

    asyncio.run(
        local_server._render_media_job(
            letter["letter_id"], letter["content"], letter["reply_text"], letter["reply_mode"]
        )
    )

    media_path = data_root / "media" / f"{letter['letter_id']}.mp4"
    assert media_path.read_bytes() == b"video"
    assert (data_root / "state.json").is_file()
    assert not (unrelated_cwd / "data").exists()
    response = asyncio.run(
        local_server.handler(make_mocked_request("GET", f"/toy/media/{media_path.name}"))
    )
    assert isinstance(response, web.FileResponse)
    assert response._path == media_path


@pytest.mark.parametrize(
    ("error_code", "expected_status", "expected_retryable"),
    [
        ("TTS_CONTENT_GATE_REJECTED", "FAILED", False),
        ("TTS_CONTENT_GATE_UNAVAILABLE", "UNAVAILABLE", True),
        (r"D:\private\voice.wav", "UNAVAILABLE", True),
    ],
)
def test_public_detail_distinguishes_directed_tts_gate_terminal_states(
    tmp_path: Path,
    monkeypatch,
    error_code: str,
    expected_status: str,
    expected_retryable: bool,
) -> None:
    letter_id = f"media-{error_code.casefold()}"
    letter = {
        "letter_id": letter_id,
        "content": "synthetic letter",
        "reply_text": "林" * 190,
        "reply_mode": ReplyMode.SPOKEN_VIDEO.value,
        "letter_status": "COMPLETED",
        "media_status": "PENDING",
        "reply_not_before": 0.0,
    }
    local_server.store.letters[:] = [letter]
    scene = tmp_path / "scene.mp4"
    scene.write_bytes(b"synthetic scene")
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("OLIVIA_ORDINARY_ACTION_BASE", str(scene))
    monkeypatch.setenv("OLIVIA_OFFICIAL_REPLY_REFERENCE", str(scene))
    monkeypatch.setenv("OLIVIA_MUSIC_PERFORMANCE_BASE", str(scene))
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)

    async def voice_plan(_letter, text):
        return VoicePerformancePlan(
            reply_text=text,
            overall_emotion="声音柔软自然地承接，再缓缓托起给到力量",
            global_speed=1.0,
            energy=0.55,
            breath_before_sentences=(),
            emphasize_sentences=(),
        )

    def fail_render(*_args, **_kwargs):
        raise local_server.ReplyMediaError(error_code)

    monkeypatch.setattr(local_server, "_voice_plan_for_letter", voice_plan)
    monkeypatch.setattr(local_server, "render_reply_video", fail_render)

    asyncio.run(
        local_server._render_media_job(
            letter_id,
            letter["content"],
            letter["reply_text"],
            ReplyMode.SPOKEN_VIDEO.value,
        )
    )
    detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"letter_id": letter_id},
        )
    )["data"]

    assert detail["media_status"] == expected_status
    assert detail["media_error_code"] == (error_code if error_code.startswith("TTS_") else "MEDIA_PROVIDER_UNAVAILABLE")
    assert detail["media_retryable"] is expected_retryable


@pytest.mark.parametrize(
    ("stored_status", "expected"),
    [
        ("QUEUED", ("PENDING", None, False)),
        ("UNAVAILABLE_DATA_ROOT_NOT_CONFIGURED", ("UNAVAILABLE", "MEDIA_PROVIDER_UNAVAILABLE", True)),
        ("UNAVAILABLE_THIRD_PARTY_NOT_INSTALLED", ("UNAVAILABLE", "MEDIA_PROVIDER_UNAVAILABLE", True)),
        ("INTERNAL_CRASH", ("UNAVAILABLE", "MEDIA_PROVIDER_UNAVAILABLE", True)),
    ],
)
def test_public_detail_projects_every_legacy_internal_media_status(
    stored_status: str,
    expected: tuple[str, str | None, bool],
) -> None:
    letter_id = f"status-{stored_status.casefold()}"
    local_server.store.letters[:] = [
        {
            "letter_id": letter_id,
            "content": "synthetic",
            "letter_status": "COMPLETED",
            "reply_text": "synthetic",
            "reply_mode": ReplyMode.SPOKEN_VIDEO.value,
            "media_status": stored_status,
            "media_error_code": r"D:\private\voice.wav" if stored_status == "INTERNAL_CRASH" else None,
            "reply_not_before": 0.0,
        }
    ]

    detail = asyncio.run(local_server.route(
        "GET", "/toy/letter/detail", {}, {"letter_id": letter_id}
    ))["data"]
    assert (detail["media_status"], detail["media_error_code"], detail["media_retryable"]) == expected


def test_internal_spoken_segment_and_complete_musical_renderers(
    tmp_path: Path,
    monkeypatch,
):
    reply_text = "I hear you, and I am staying with you through this."
    plan = VoicePerformancePlan(
        reply_text=reply_text,
        overall_emotion="restrained empathy becoming steady reassurance",
        global_speed=1.06,
        energy=0.62,
        breath_before_sentences=(),
        emphasize_sentences=(1,),
    )
    scene = tmp_path / "scene.mp4"
    scene.write_bytes(b"scene")
    official = tmp_path / "official.mp4"
    official.write_bytes(b"official")
    letters = [
        {
            "letter_id": "spoken-entry",
            "content": "ordinary video request",
            "reply_text": reply_text,
            "reply_mode": "spoken_video",
        },
        {
            "letter_id": "musical-entry",
            "content": "spoken plus music request",
            "reply_text": reply_text,
            "reply_mode": "musical_video",
            "music_duration_seconds": 60,
        },
    ]
    local_server.store.letters[:] = letters
    monkeypatch.setenv("OLIVIA_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("OLIVIA_OFFICIAL_REPLY_REFERENCE", str(official))
    monkeypatch.setenv("OLIVIA_ORDINARY_ACTION_BASE", str(scene))
    monkeypatch.setenv("OLIVIA_MUSIC_PERFORMANCE_BASE", str(scene))
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)

    directed_requests = []

    async def direct_frozen_reply(
        text,
        gateway,
        *,
        letter_content=None,
        request_id=None,
        persona_snapshot=None,
        mode=None,
    ):
        assert text == reply_text
        assert gateway is local_server.letters_adapter.gateway
        assert letter_content in {
            "ordinary video request",
            "spoken plus music request",
        }
        assert persona_snapshot.status == "READY"
        directed_requests.append((request_id, mode))
        return plan

    received = {}

    def render_spoken(_text, output, **kwargs):
        received["ordinary video request"] = (
            kwargs["voice_performance_plan"], kwargs["scene_path"]
        )
        Path(output).write_bytes(b"spoken-video")

    def render_musical(content, _text, output, **kwargs):
        received[content] = (
            kwargs["voice_performance_plan"],
            kwargs["spoken_action_base_path"],
        )
        Path(output).write_bytes(b"spoken-transition-music")
        return {
            "audio_provider": "breeze_tts2",
            "reply_structure": (
                "normal_video_then_official_transition_then_song_video"
            ),
            "song_emotion": "gentle_reassurance",
            "transition_duration_seconds": 8.0,
            "lyrics": "private generated lyric",
            "instruction": "private voice direction",
        }

    monkeypatch.setattr(local_server, "direct_voice_performance", direct_frozen_reply)
    monkeypatch.setattr(local_server, "render_reply_video", render_spoken)
    monkeypatch.setattr(local_server, "render_musical_reply", render_musical)

    async def exercise():
        await local_server._render_media_job(
            "spoken-entry", "ordinary video request", reply_text, "spoken_video"
        )
        await local_server._render_media_job(
            "musical-entry", "spoken plus music request", reply_text, "musical_video"
        )

    asyncio.run(exercise())

    assert received == {
        "ordinary video request": (plan, scene),
        "spoken plus music request": (plan, scene),
    }
    assert all(letter["media_status"] == "COMPLETED" for letter in letters)
    assert directed_requests == [
        ("letter-reply:spoken-entry:voice-direction", ReplyMode.SPOKEN_VIDEO),
        ("letter-reply:musical-entry:voice-direction", ReplyMode.MUSICAL_VIDEO),
    ]
    assert letters[0]["voice_performance_plan"] == plan.to_dict()
    assert letters[1]["voice_performance_plan"] == plan.to_dict()
    assert {
        key: letters[1].get(key)
        for key in (
            "audio_provider",
            "reply_structure",
            "song_emotion",
            "transition_seconds",
        )
    } == {
        "audio_provider": "breeze_tts2",
        "reply_structure": "normal_video_then_official_transition_then_song_video",
        "song_emotion": "gentle_reassurance",
        "transition_seconds": 8.0,
    }
    assert "lyrics" not in letters[1]
    assert "instruction" not in letters[1]

    detail = asyncio.run(
        local_server.route(
            "GET",
            "/toy/letter/detail",
            {},
            {"letter_id": "musical-entry"},
        )
    )["data"]
    assert detail["audio_provider"] == "breeze_tts2"
    assert detail["reply_structure"] == "normal_video_then_official_transition_then_song_video"
    assert detail["song_emotion"] == "gentle_reassurance"
    assert detail["transition_seconds"] == 8.0
    assert "lyrics" not in detail
    assert "instruction" not in detail


def test_corrupt_persisted_voice_plan_fails_closed_without_redirection(monkeypatch):
    letter = {
        "letter_id": "corrupt-plan",
        "voice_performance_plan": {"reply_text": "frozen reply"},
    }
    provider_calls = []

    async def direct(_text, _gateway, *, letter_content=None, request_id=None):
        provider_calls.append(request_id)
        raise AssertionError("corrupt state must not call the voice provider")

    monkeypatch.setattr(local_server, "direct_voice_performance", direct)

    with pytest.raises(VoiceDirectionError, match="VOICE_DIRECTION_PERSISTED_PLAN_INVALID"):
        asyncio.run(local_server._voice_plan_for_letter(letter, "frozen reply"))

    assert provider_calls == []


def test_music_voice_plan_uses_llm_direction_and_persists_it(monkeypatch):
    reply_text = "The frozen reply has two sentences. This is the second one."
    letter = {"letter_id": "music-directed", "content": "music letter"}
    expected = VoicePerformancePlan(
        reply_text=reply_text,
        overall_emotion="quiet concern becoming grounded reassurance",
        global_speed=1.06,
        energy=0.61,
        breath_before_sentences=(),
        emphasize_sentences=(1,),
    )
    calls = []

    async def direct(
        text,
        gateway,
        *,
        letter_content=None,
        request_id=None,
        persona_snapshot=None,
        mode=None,
    ):
        calls.append(
            (text, gateway, letter_content, request_id, persona_snapshot, mode)
        )
        return expected

    monkeypatch.setattr(local_server, "direct_voice_performance", direct)

    plan = asyncio.run(local_server._music_voice_plan_for_letter(letter, reply_text))

    assert plan == expected
    assert len(calls) == 1
    assert calls[0][:4] == (
        reply_text,
        local_server.letters_adapter.gateway,
        "music letter",
        "letter-reply:music-directed:voice-direction",
    )
    assert calls[0][4].status == "READY"
    assert calls[0][5] is ReplyMode.MUSICAL_VIDEO
    assert letter["voice_performance_plan"] == expected.to_dict()



def test_voice_direction_retries_keep_a_persisted_provider_idempotency_key(monkeypatch):
    reply_text = "This reply is frozen before voice direction."
    request_id = "letter-reply:durable-direction:voice-direction"
    letter = {"letter_id": "durable-direction"}
    provider_calls = []
    persisted_request_ids = []
    plan = VoicePerformancePlan(
        reply_text=reply_text,
        overall_emotion="steady reassurance",
        global_speed=1.06,
        energy=0.5,
        breath_before_sentences=(),
        emphasize_sentences=(),
    )

    def persist():
        persisted_request_ids.append(letter.get("voice_direction_request_id"))

    async def direct(
        _text,
        _gateway,
        *,
        letter_content=None,
        request_id=None,
        persona_snapshot=None,
        mode=None,
    ):
        provider_calls.append(request_id)
        assert persona_snapshot.status == "READY"
        assert mode is ReplyMode.SPOKEN_VIDEO
        assert letter["voice_direction_request_id"] == request_id
        if len(provider_calls) == 1:
            raise asyncio.CancelledError()
        return plan

    monkeypatch.setattr(local_server, "_persist_media_state", persist)
    monkeypatch.setattr(local_server, "direct_voice_performance", direct)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(local_server._voice_plan_for_letter(letter, reply_text))
    assert persisted_request_ids == [request_id]
    assert provider_calls == [request_id]

    assert asyncio.run(local_server._voice_plan_for_letter(letter, reply_text)) == plan
    assert asyncio.run(local_server._voice_plan_for_letter(letter, reply_text)) == plan
    assert provider_calls == [request_id, request_id]
    assert letter["voice_performance_plan"] == plan.to_dict()


def test_startup_resumes_only_valid_pending_or_interrupted_media_jobs(monkeypatch):
    local_server.store.letters[:] = [
        {
            "letter_id": "resume-spoken",
            "content": "saved letter",
            "reply_text": "saved reply",
            "reply_mode": ReplyMode.SPOKEN_VIDEO.value,
            "letter_status": "COMPLETED",
            "media_status": "PENDING",
        },
        {
            "letter_id": "resume-musical",
            "content": "saved musical letter",
            "reply_text": "saved musical reply",
            "reply_mode": ReplyMode.MUSICAL_VIDEO.value,
            "letter_status": "COMPLETED",
            "media_status": "QUEUED",
        },
        {
            "letter_id": "not-video",
            "content": "saved text letter",
            "reply_text": "saved text reply",
            "reply_mode": ReplyMode.TEXT_LETTER.value,
            "letter_status": "COMPLETED",
            "media_status": "PENDING",
        },
        {
            "letter_id": "not-complete",
            "content": "incomplete letter",
            "reply_text": "",
            "reply_mode": ReplyMode.SPOKEN_VIDEO.value,
            "letter_status": "PENDING",
            "media_status": "QUEUED",
        },
    ]
    scheduled = []
    monkeypatch.setattr(local_server, "_schedule_pending_reply_jobs", lambda: 0)
    monkeypatch.setattr(
        local_server,
        "_schedule_media_job",
        lambda letter_id, content, reply_text, mode: scheduled.append(
            (letter_id, content, reply_text, mode)
        ),
    )

    asyncio.run(local_server._start_reply_tasks(web.Application()))

    assert scheduled == [
        ("resume-spoken", "saved letter", "saved reply", ReplyMode.MUSICAL_VIDEO.value),
        ("resume-musical", "saved musical letter", "saved musical reply", ReplyMode.MUSICAL_VIDEO.value),
    ]


def test_shutdown_cancels_and_releases_owned_media_jobs():
    async def exercise() -> bool:
        waiting = asyncio.Event()

        async def media_job():
            await waiting.wait()

        task = asyncio.create_task(media_job())
        local_server.media_tasks.add(task)
        local_server.media_jobs["cleanup-media"] = task
        await asyncio.sleep(0)
        await local_server._stop_reply_tasks(web.Application())
        return task.cancelled()

    assert asyncio.run(exercise())
    assert local_server.media_tasks == set()
    assert local_server.media_jobs == {}


def test_reply_pipeline_total_timeout_covers_all_quality_stages(monkeypatch):
    config = GatewayConfig(
        provider="openai_compatible",
        api_style="chat_completions",
        model="synthetic-model",
        timeout_seconds=90.0,
    )
    monkeypatch.setattr(local_server, "LLM_CONFIG", config)
    monkeypatch.setattr(local_server, "LLM_TIMEOUT_SECONDS", 90.0)
    monkeypatch.delenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", raising=False)

    assert local_server._reply_pipeline_timeout_seconds(ReplyMode.TEXT_LETTER.value) == 515.0

    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", "20")
    assert local_server._reply_pipeline_timeout_seconds(ReplyMode.TEXT_LETTER.value) == 235.0


def test_deepseek_flash_reply_pipeline_timeout_covers_reasoning_and_adjudication(
    monkeypatch,
):
    config = GatewayConfig(
        provider="openai_compatible",
        api_style="chat_completions",
        model="deepseek-v4-flash",
        timeout_seconds=180.0,
        reasoning_timeout_seconds=720.0,
    )
    monkeypatch.setattr(local_server, "LLM_CONFIG", config)
    monkeypatch.setattr(local_server, "LLM_TIMEOUT_SECONDS", 180.0)
    monkeypatch.delenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", raising=False)

    assert local_server._reply_pipeline_timeout_seconds(ReplyMode.TEXT_LETTER.value) == 5765.0
    assert local_server._reply_pipeline_timeout_seconds(ReplyMode.SPOKEN_VIDEO.value) == 365.0

    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", "20")
    assert local_server._reply_pipeline_timeout_seconds(ReplyMode.TEXT_LETTER.value) == 5765.0
    assert local_server._reply_pipeline_timeout_seconds(ReplyMode.SPOKEN_VIDEO.value) == 245.0


def test_explicit_deepseek_reviewer_extends_non_deepseek_outer_pipeline_budget(
    monkeypatch,
):
    config = GatewayConfig(
        provider="openai_compatible",
        api_style="chat_completions",
        model="vendor/not-deepseek",
        timeout_seconds=90.0,
        reasoning_timeout_seconds=600.0,
    )
    monkeypatch.setattr(local_server, "LLM_CONFIG", config)
    monkeypatch.setattr(local_server, "LLM_TIMEOUT_SECONDS", 90.0)
    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", "20")

    assert local_server._reply_pipeline_timeout_seconds(
        ReplyMode.TEXT_LETTER.value
    ) == 4295.0


def test_run_reply_pipeline_scopes_only_text_letter_generation_for_max_reasoning(
    monkeypatch,
):
    seen = []

    class RecordingPipeline:
        async def run(self, request, context):
            seen.append((request, context.mode))
            return object()

    config = GatewayConfig(
        provider="openai_compatible",
        api_style="chat_completions",
        model="deepseek-v4-flash",
        timeout_seconds=180.0,
        reasoning_timeout_seconds=720.0,
    )
    monkeypatch.setattr(local_server, "LLM_CONFIG", config)
    monkeypatch.setattr(local_server, "LLM_TIMEOUT_SECONDS", 180.0)
    monkeypatch.setattr(local_server, "reply_pipeline", RecordingPipeline())

    async def exercise():
        await local_server._run_reply_pipeline_for_letter(
            {"letter_id": "text-fixture"},
            "synthetic text letter",
            ReplyMode.TEXT_LETTER.value,
            idempotency_key="stable",
        )
        await local_server._run_reply_pipeline_for_letter(
            {"letter_id": "video-fixture"},
            "synthetic video request",
            ReplyMode.SPOKEN_VIDEO.value,
            idempotency_key="stable",
        )

    asyncio.run(exercise())

    assert seen[0][0].gateway_scope is GatewayRequestScope.TEXT_LETTER_MAX_REASONING
    assert seen[0][0].request_id == "letter-reply:text-fixture"
    assert seen[0][0].idempotency_key == "stable:text-fixture"
    assert seen[0][1] is ReplyMode.TEXT_LETTER
    assert seen[1][0].gateway_scope is None
    assert seen[1][0].request_id == "letter-reply:video-fixture"
    assert seen[1][0].idempotency_key == "stable:video-fixture"
    assert seen[1][1] is ReplyMode.SPOKEN_VIDEO


@pytest.mark.parametrize(
    ("routed_mode", "decision", "delivery_mode", "expected_media"),
    (
        (
            ReplyMode.TEXT_LETTER.value,
            TriageResult(
                "normal",
                ReplyMode.TEXT_LETTER.value,
                "direct_words_are_enough",
                "completed",
                True,
            ),
            ReplyMode.TEXT_LETTER.value,
            (),
        ),
        (
            ReplyMode.SPOKEN_VIDEO.value,
            TriageResult(
                "high",
                ReplyMode.SPOKEN_VIDEO.value,
                "voice_adds_presence",
                "completed",
                True,
                direct_response_sufficient=True,
                voice_materially_better=True,
            ),
            ReplyMode.MUSICAL_VIDEO.value,
            (ReplyMode.MUSICAL_VIDEO.value,),
        ),
        (
            ReplyMode.MUSICAL_VIDEO.value,
            TriageResult(
                "mixed",
                ReplyMode.MUSICAL_VIDEO.value,
                "melody_carries_this_reply",
                "completed",
                True,
                music_contexts=("melody_idea",),
                music_intent="compose",
                direct_response_sufficient=False,
                music_materially_better=True,
                character_willing=True,
            ),
            ReplyMode.MUSICAL_VIDEO.value,
            (ReplyMode.MUSICAL_VIDEO.value,),
        ),
    ),
)
def test_generate_reply_preserves_the_router_selected_video_route(
    monkeypatch,
    routed_mode,
    decision,
    delivery_mode,
    expected_media,
):
    letter_id = f"synthetic-{routed_mode}"
    letter = {
        "letter_id": letter_id,
        "content": "synthetic current letter",
        "reply_text": "",
        "reply_mode": ReplyMode.TEXT_LETTER.value,
        "letter_status": "PENDING",
    }
    local_server.store.letters[:] = [letter]
    observed = {}
    scheduled = []

    async def classify(_content):
        return decision

    async def run_pipeline(request, context):
        observed["request_content"] = request.content
        observed["context_mode"] = context.mode.value
        return PipelineResult(
            letter_id,
            ReplyState.COMPLETED,
            text="林" * 190,
            quality_status="accepted_degraded",
        )

    monkeypatch.setattr(local_server.emotion_triage, "classify", classify)
    monkeypatch.setattr(local_server.reply_pipeline, "run", run_pipeline)
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(local_server, "_commit_private_world_letter", lambda _letter: False)
    monkeypatch.setattr(
        local_server,
        "_schedule_media_job",
        lambda _letter_id, _content, _reply, mode: scheduled.append(mode),
    )
    monkeypatch.setattr(
        local_server.letters_adapter,
        "remember_conversation",
        lambda _content, _reply: None,
    )

    assert asyncio.run(
        local_server.generate_reply(letter_id, "synthetic current letter")
    )
    assert observed["context_mode"] == delivery_mode
    assert ("<ordinary_video_reply_constraints>" in observed["request_content"]) is (
        delivery_mode != ReplyMode.TEXT_LETTER.value
    )
    assert letter["reply_mode"] == delivery_mode
    assert tuple(scheduled) == expected_media

    public = local_server.letter_to_out(letter)
    assert public["reply_mode_exact"] == delivery_mode
    assert public["reply_mode"] == (
        "text" if delivery_mode == ReplyMode.TEXT_LETTER.value else "video"
    )


@pytest.mark.parametrize(
    "video_mode",
    (ReplyMode.SPOKEN_VIDEO.value, ReplyMode.MUSICAL_VIDEO.value),
)
def test_generate_reply_repairs_then_rechecks_video_copy_length(
    monkeypatch,
    video_mode,
):
    letter_id = f"synthetic-duration-repair-{video_mode}"
    letter = {
        "letter_id": letter_id,
        "content": "synthetic current letter",
        "reply_text": "",
        "reply_mode": ReplyMode.TEXT_LETTER.value,
        "letter_status": "PENDING",
    }
    local_server.store.letters[:] = [letter]
    requests = []

    async def classify(_content):
        return TriageResult(
            "high",
            video_mode,
            "voice_adds_presence",
            "completed",
            True,
            direct_response_sufficient=True,
            voice_materially_better=True,
        )

    async def run_pipeline(request, _context):
        requests.append(request)
        if request.request_id.endswith(":duration-repair"):
            return PipelineResult(
                letter_id,
                ReplyState.COMPLETED,
                text="林" * 190,
                quality_status="accepted",
            )
        return PipelineResult(
            letter_id,
            ReplyState.FAILED,
            text="太短",
            error_code="REWRITE_FAILED",
            quality_status="blocked",
            violation_codes=(
                "VIDEO_REPLY_LENGTH_OUT_OF_RANGE",
                "MEMORY_FABRICATION",
            ),
            reviewer_calls=1,
            rewrite_calls=1,
            delivery_repair_disposition=(
                DeliveryRepairDisposition.VIDEO_LENGTH
            ),
        )

    monkeypatch.setattr(local_server.emotion_triage, "classify", classify)
    monkeypatch.setattr(local_server.reply_pipeline, "run", run_pipeline)
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(local_server, "_commit_private_world_letter", lambda _letter: False)
    monkeypatch.setattr(local_server, "_schedule_media_job", lambda *_args: None)
    monkeypatch.setattr(local_server.letters_adapter, "remember_conversation", lambda *_args: None)

    assert asyncio.run(local_server.generate_reply(letter_id, letter["content"]))
    assert [request.request_id for request in requests] == [
        f"letter-reply:{letter_id}",
        f"letter-reply:{letter_id}:duration-repair",
    ]
    assert "180到200个非空白字符" in requests[1].content
    assert "汉字、标点、数字和英文字母均计入" in requests[1].content
    assert "空格和换行不计入" in requests[1].content
    assert letter["reply_text"] == "林" * 190


def test_generate_reply_does_not_repair_hard_memory_video_failure(
    monkeypatch,
):
    letter_id = "synthetic-hard-memory-video"
    letter = {
        "letter_id": letter_id,
        "content": "synthetic current letter",
        "reply_text": "",
        "reply_mode": ReplyMode.TEXT_LETTER.value,
        "letter_status": "PENDING",
    }
    local_server.store.letters[:] = [letter]
    requests = []
    scheduled = []

    async def classify(_content):
        return TriageResult(
            "high",
            ReplyMode.MUSICAL_VIDEO.value,
            "melody_carries_this_reply",
            "completed",
            True,
            music_contexts=("melody_idea",),
            music_intent="compose",
            music_materially_better=True,
            character_willing=True,
        )

    async def run_pipeline(request, _context):
        requests.append(request)
        return PipelineResult(
            letter_id,
            ReplyState.FAILED,
            text="太短",
            error_code="REPLY_QUALITY_BLOCKED",
            quality_status="blocked",
            violation_codes=(
                "VIDEO_REPLY_LENGTH_OUT_OF_RANGE",
                "MEMORY_FABRICATION",
            ),
            delivery_repair_disposition=DeliveryRepairDisposition.NONE,
        )

    monkeypatch.setattr(local_server.emotion_triage, "classify", classify)
    monkeypatch.setattr(local_server.reply_pipeline, "run", run_pipeline)
    monkeypatch.setattr(local_server, "_persist_store_state", lambda: None)
    monkeypatch.setattr(
        local_server,
        "_schedule_media_job",
        lambda *_args: scheduled.append(True),
    )

    assert not asyncio.run(
        local_server.generate_reply(letter_id, letter["content"])
    )
    assert [request.request_id for request in requests] == [
        f"letter-reply:{letter_id}"
    ]
    assert scheduled == []
    assert letter["media_status"] == "NOT_REQUESTED"

@pytest.fixture(autouse=True)
def _eligible_breeze_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_server, "require_breeze_hardware", lambda: None)
