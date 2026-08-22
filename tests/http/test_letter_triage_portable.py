from __future__ import annotations

import asyncio
import json

from letter_triage import LetterReplyRouter


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _Gateway:
    def __init__(self, response: str) -> None:
        self.response = response

    async def complete(self, _messages, *, request_id=None):
        return _Response(self.response)


def _route(**overrides):
    payload = {
        "mode": "text_letter",
        "reason_code": "direct_words_are_enough",
        "emotion_level": "normal",
        "music_contexts": [],
        "music_intent": "none",
        "direct_response_sufficient": True,
        "voice_materially_better": False,
        "music_materially_better": False,
        "character_willing": True,
    }
    payload.update(overrides)
    return asyncio.run(
        LetterReplyRouter(_Gateway(json.dumps(payload))).classify(
            "synthetic current letter"
        )
    )


def test_high_emotion_can_choose_direct_spoken_video_without_music():
    result = _route(
        mode="spoken_video",
        reason_code="voice_adds_presence",
        emotion_level="high",
        direct_response_sufficient=True,
        voice_materially_better=True,
    )

    assert result.reply_mode == "spoken_video"
    assert result.emotion_level == "high"
    assert result.music_contexts == ()


def test_music_discussion_does_not_automatically_trigger_performance():
    result = _route(
        reason_code="music_topic_still_needs_words",
        music_contexts=["music_discussion"],
        music_intent="discuss",
    )

    assert result.reply_mode == "text_letter"
    assert result.music_intent == "discuss"


def test_explicit_performance_request_can_be_refused_or_deferred():
    result = _route(
        reason_code="not_willing_to_perform_now",
        music_contexts=["explicit_performance_or_adaptation_request"],
        music_intent="discuss",
        character_willing=False,
    )

    assert result.reply_mode == "text_letter"
    assert result.character_willing is False


def test_musical_video_requires_context_intent_better_fit_and_willingness():
    invalid = _route(
        mode="musical_video",
        reason_code="request_only_is_not_enough",
        music_contexts=["explicit_performance_or_adaptation_request"],
        music_intent="perform",
        direct_response_sufficient=False,
        music_materially_better=False,
        character_willing=True,
    )

    assert invalid.reply_mode == "text_letter"
    assert invalid.status == "unavailable"
    assert invalid.reason_code == "router_invalid_result"


def test_valid_musical_video_is_a_character_choice_not_a_keyword_trigger():
    result = _route(
        mode="musical_video",
        reason_code="melody_carries_this_reply",
        emotion_level="mixed",
        music_contexts=["melody_idea", "emotion_music_fit"],
        music_intent="compose",
        direct_response_sufficient=False,
        music_materially_better=True,
        character_willing=True,
    )

    assert result.reply_mode == "musical_video"
    assert result.status == "completed"
    assert result.music_intent == "compose"


def test_invalid_router_output_fails_closed_to_text_letter():
    result = asyncio.run(
        LetterReplyRouter(_Gateway("not-json")).classify("普通聊天")
    )

    assert result.reply_mode == "text_letter"
    assert result.status == "unavailable"
