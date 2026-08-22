from __future__ import annotations

import asyncio

import pytest

import local_server
from letter_triage import TriageResult
from reply_context import ReplyMode
from reply_orchestrator import ReplyState
from reply_pipeline import PipelineResult


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


@pytest.mark.parametrize(
    ("exact_mode", "decision", "expected_media"),
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
            (ReplyMode.MUSICAL_VIDEO.value,),
        ),
    ),
)
def test_generate_reply_carries_one_exact_mode_through_context_and_media(
    monkeypatch,
    exact_mode,
    decision,
    expected_media,
):
    letter_id = f"synthetic-{exact_mode}"
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

    async def run_pipeline(_request, context):
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
    assert observed["context_mode"] == exact_mode
    assert letter["reply_mode"] == exact_mode
    assert tuple(scheduled) == expected_media

    public = local_server.letter_to_out(letter)
    assert public["reply_mode_exact"] == exact_mode
    assert public["reply_mode"] == (
        "text" if exact_mode == ReplyMode.TEXT_LETTER.value else "video"
    )
