from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web

import local_server
from letter_triage import TriageResult
from reply_context import ReplyMode
from reply_orchestrator import ReplyState
from reply_pipeline import PipelineResult
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
    monkeypatch.setenv("OLIVIA_SCENE_DAY", str(scene))
    monkeypatch.setenv("OLIVIA_MUSIC_SCENE_DAY", str(scene))
    monkeypatch.setenv("OLIVIA_MUSIC_SCENE_DUSK", str(scene))
    monkeypatch.setenv("OLIVIA_MUSIC_SCENE_NIGHT", str(scene))
    monkeypatch.setenv("OLIVIA_OFFICIAL_REPLY_REFERENCE", str(official_reference))
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
        )

    def render(_content, _reply, output, **kwargs):
        observed.update(kwargs)
        Path(output).write_bytes(b"final-video")
        return {}

    monkeypatch.setattr(local_server, "render_musical_reply", render)
    monkeypatch.setattr(local_server, "_voice_plan_for_letter", voice_plan)

    asyncio.run(
        local_server._render_media_job(
            "retry-media", "letter", "reply", "musical_video"
        )
    )

    assert letter["media_status"] == "COMPLETED"
    assert letter["media_error_code"] is None
    assert observed["official_reply_reference_path"] == official_reference
    assert observed["performance_video_path"] == scene
    assert "normal_scene_path" not in observed
    assert observed["normal_video_path"].name.endswith("-official-spoken-v1.mp4")
    assert observed["song_video_path"].name.endswith("-song-v2-60s.mp4")


def test_both_product_video_renderers_receive_the_persisted_llm_voice_plan(
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
    for key in ("MORNING", "DAY", "DUSK", "NIGHT"):
        monkeypatch.setenv(f"OLIVIA_SCENE_{key}", str(scene))
        monkeypatch.setenv(f"OLIVIA_MUSIC_SCENE_{key}", str(scene))
    monkeypatch.setattr(local_server, "_persist_media_state", lambda: None)

    directed_requests = []

    async def direct_frozen_reply(text, gateway, *, request_id=None):
        assert text == reply_text
        assert gateway is local_server.letters_adapter.gateway
        directed_requests.append(request_id)
        return plan

    received = {}

    def render_spoken(_text, output, **kwargs):
        received["spoken_video"] = kwargs["voice_performance_plan"]
        Path(output).write_bytes(b"spoken")
        return {}

    def render_musical(_content, _text, output, **kwargs):
        received["musical_video"] = kwargs["voice_performance_plan"]
        Path(output).write_bytes(b"musical")
        return {}

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

    assert received == {"spoken_video": plan, "musical_video": plan}
    assert directed_requests == [
        "letter-reply:spoken-entry:voice-direction",
        "letter-reply:musical-entry:voice-direction",
    ]
    assert letters[0]["voice_performance_plan"] == plan.to_dict()
    assert letters[1]["voice_performance_plan"] == plan.to_dict()


def test_corrupt_persisted_voice_plan_fails_closed_without_redirection(monkeypatch):
    letter = {
        "letter_id": "corrupt-plan",
        "voice_performance_plan": {"reply_text": "frozen reply"},
    }
    provider_calls = []

    async def direct(_text, _gateway, *, request_id=None):
        provider_calls.append(request_id)
        raise AssertionError("corrupt state must not call the voice provider")

    monkeypatch.setattr(local_server, "direct_voice_performance", direct)

    with pytest.raises(VoiceDirectionError, match="VOICE_DIRECTION_PERSISTED_PLAN_INVALID"):
        asyncio.run(local_server._voice_plan_for_letter(letter, "frozen reply"))

    assert provider_calls == []


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

    async def direct(_text, _gateway, *, request_id=None):
        provider_calls.append(request_id)
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
        ("resume-spoken", "saved letter", "saved reply", ReplyMode.SPOKEN_VIDEO.value),
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
    monkeypatch.setattr(local_server, "LLM_TIMEOUT_SECONDS", 90.0)
    monkeypatch.delenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", raising=False)

    assert local_server._reply_pipeline_timeout_seconds() == 275.0

    monkeypatch.setenv("OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS", "20")
    assert local_server._reply_pipeline_timeout_seconds() == 155.0


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
            ReplyMode.SPOKEN_VIDEO.value,
            (ReplyMode.SPOKEN_VIDEO.value,),
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
            text="synthetic canonical reply",
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
