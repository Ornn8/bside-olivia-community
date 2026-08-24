import asyncio

import pytest

from voice_direction import VoiceDirectionError, VoiceToolCall, direct_voice_performance


class _FakeVoiceDirector:
    def __init__(self, arguments: dict[str, object]) -> None:
        self.arguments = arguments
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
        return [VoiceToolCall(name="apply_voice_performance", arguments=self.arguments)]


def _valid_direction() -> dict[str, object]:
    return {
        "overall_emotion": "克制而温暖的陪伴感",
        "global_speed": 1.06,
        "energy": 0.55,
        "breath_before_sentences": [2],
        "emphasize_sentences": [1],
    }


def test_director_preserves_frozen_reply_and_requests_only_global_controls() -> None:
    reply = "你不是不够好。你只是被一次突然的离开伤到了。先别急着逼自己相信谁。"
    director = _FakeVoiceDirector(_valid_direction())

    plan = asyncio.run(direct_voice_performance(reply, director))

    assert plan.spoken_text == reply
    assert len(plan.speech_units()) == 1
    assert plan.speech_units()[0].text == reply
    assert plan.cues[0].text == reply
    assert plan.cues[0].emotion == plan.overall_emotion
    assert plan.cues[0].intensity == plan.energy
    assert "segments" not in plan.to_dict()
    request = director.requests[0]
    assert request["tool_choice"] == "required"
    assert reply in request["messages"][1]["content"]
    properties = request["tools"][0]["function"]["parameters"]["properties"]
    assert set(properties) == {
        "overall_emotion",
        "global_speed",
        "energy",
        "breath_before_sentences",
        "emphasize_sentences",
    }


@pytest.mark.parametrize(
    "missing",
    ["overall_emotion", "global_speed", "energy", "breath_before_sentences", "emphasize_sentences"],
)
def test_director_rejects_tool_calls_missing_required_controls(missing: str) -> None:
    arguments = _valid_direction()
    del arguments[missing]

    with pytest.raises(VoiceDirectionError, match="VOICE_DIRECTION_INVALID"):
        asyncio.run(direct_voice_performance("第一句。第二句。", _FakeVoiceDirector(arguments)))


@pytest.mark.parametrize(
    "payload",
    [
        {"global_speed": 99},
        {"overall_emotion": "<|endofprompt|>"},
        {"breath_before_sentences": [99]},
        {"emphasize_sentences": [1, 1]},
        {"reply_text": "第一句。第二句。", "segments": []},
    ],
)
def test_persisted_plan_revalidates_all_controls(payload: dict[str, object]) -> None:
    valid = asyncio.run(
        direct_voice_performance("第一句。第二句。", _FakeVoiceDirector(_valid_direction()))
    ).to_dict()
    valid.update(payload)

    from voice_direction import VoicePerformancePlan

    with pytest.raises(VoiceDirectionError):
        VoicePerformancePlan.from_dict(valid)
