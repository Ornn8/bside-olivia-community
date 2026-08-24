import asyncio

import pytest

from voice_direction import (
    VoiceDirectionError,
    VoiceToolCall,
    direct_voice_performance,
)


class _FakeVoiceDirector:
    def __init__(self, call: VoiceToolCall) -> None:
        self.call = call
        self.requests: list[dict[str, object]] = []

    async def complete_with_tools(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, object]],
        tool_choice: str,
    ) -> list[VoiceToolCall]:
        self.requests.append(
            {"messages": messages, "tools": tools, "tool_choice": tool_choice}
        )
        return [self.call]


def test_director_uses_frozen_reply_as_context_and_preserves_it_exactly() -> None:
    reply = "你不是不够好。你只是被一次突然的离开伤到了。先别急着逼自己相信谁。"
    director = _FakeVoiceDirector(
        VoiceToolCall(
            name="apply_voice_performance",
            arguments={
                "segments": [
                    {
                        "sentence_start": 1,
                        "sentence_end": 1,
                        "emotion": "克制的心疼",
                        "intensity": 0.46,
                        "speed": 0.99,
                        "pause_after_ms": 260,
                        "gain_db": -0.2,
                    },
                    {
                        "sentence_start": 2,
                        "sentence_end": 3,
                        "emotion": "温和但笃定",
                        "intensity": 0.58,
                        "speed": 1.01,
                        "pause_after_ms": 0,
                        "gain_db": 0.1,
                    },
                ]
            },
        )
    )

    plan = asyncio.run(direct_voice_performance(reply, director))

    assert plan.spoken_text == reply
    assert plan.source == "llm_tool_call"
    assert plan.control_channel == "non_spoken"
    assert [segment.text for segment in plan.segments] == [
        "你不是不够好。",
        "你只是被一次突然的离开伤到了。先别急着逼自己相信谁。",
    ]
    request = director.requests[0]
    assert request["tool_choice"] == "required"
    assert reply in request["messages"][1]["content"]
    assert "emotion" in str(request["tools"])


@pytest.mark.parametrize(
    "segments",
    [
        [
            {
                "sentence_start": 2,
                "sentence_end": 3,
                "emotion": "温和",
                "intensity": 0.5,
                "speed": 1.0,
                "pause_after_ms": 0,
                "gain_db": 0.0,
            }
        ],
        [
            {
                "sentence_start": 1,
                "sentence_end": 2,
                "emotion": "温和",
                "intensity": 0.5,
                "speed": 1.0,
                "pause_after_ms": 0,
                "gain_db": 0.0,
            },
            {
                "sentence_start": 2,
                "sentence_end": 3,
                "emotion": "笃定",
                "intensity": 0.5,
                "speed": 1.0,
                "pause_after_ms": 0,
                "gain_db": 0.0,
            },
        ],
    ],
)
def test_director_rejects_uncovered_or_overlapping_sentence_ranges(
    segments: list[dict[str, object]],
) -> None:
    director = _FakeVoiceDirector(
        VoiceToolCall(name="apply_voice_performance", arguments={"segments": segments})
    )

    with pytest.raises(VoiceDirectionError, match="cover every sentence exactly once"):
        asyncio.run(direct_voice_performance("第一句。第二句。第三句。", director))


def test_director_rejects_plain_text_instead_of_required_tool_call() -> None:
    class _NoToolDirector:
        async def complete_with_tools(self, **_: object) -> list[VoiceToolCall]:
            return []

    with pytest.raises(VoiceDirectionError, match="exactly one tool call"):
        asyncio.run(direct_voice_performance("这是一句完整的话。", _NoToolDirector()))
