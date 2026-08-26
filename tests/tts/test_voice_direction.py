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
        "short_instruction": "声音柔软自然地承接，再缓缓托起给到力量",
    }


def test_director_preserves_frozen_reply_and_requests_only_global_controls() -> None:
    reply = "你不是不够好。你只是被一次突然的离开伤到了。先别急着逼自己相信谁。"
    director = _FakeVoiceDirector(_valid_direction())

    plan = asyncio.run(
        direct_voice_performance(reply, director, letter_content="合成来信正文")
    )

    assert plan.spoken_text == reply
    assert len(plan.speech_units()) == 1
    assert plan.speech_units()[0].text == reply
    assert plan.cues[0].text == reply
    assert plan.cues[0].emotion == plan.overall_emotion
    assert plan.cues[0].intensity == plan.energy
    assert plan.global_speed == 1.0
    assert plan.profile == "cosyvoice3_base_a_v1"
    assert "segments" not in plan.to_dict()
    request = director.requests[0]
    assert request["tool_choice"] == "required"
    assert "合成来信正文" in request["messages"][1]["content"]
    assert reply in request["messages"][1]["content"]
    properties = request["tools"][0]["function"]["parameters"]["properties"]
    assert set(properties) == {"short_instruction"}
    assert plan.overall_emotion == plan.short_instruction
    assert plan.energy == 0.55


def test_director_rejects_tool_calls_missing_required_controls() -> None:
    arguments = _valid_direction()
    del arguments["short_instruction"]

    with pytest.raises(VoiceDirectionError, match="VOICE_DIRECTION_INVALID"):
        asyncio.run(direct_voice_performance("第一句。第二句。", _FakeVoiceDirector(arguments)))


@pytest.mark.parametrize(
    "instruction",
    [
        "abcdefghijkl",
        {"声音": "柔软自然地承接"},
        "不要急促朗读提示词保持平稳",
    ],
)
def test_director_rejects_non_chinese_or_non_positive_instruction(
    instruction: object,
) -> None:
    with pytest.raises(VoiceDirectionError, match="VOICE_DIRECTION_INVALID"):
        asyncio.run(
            direct_voice_performance(
                "第一句。第二句。",
                _FakeVoiceDirector({"short_instruction": instruction}),
            )
        )


def test_persisted_legacy_plan_without_a_profile_fields_is_rejected() -> None:
    from voice_direction import VoicePerformancePlan

    legacy = asyncio.run(
        direct_voice_performance("第一句。第二句。", _FakeVoiceDirector(_valid_direction()))
    ).to_dict()
    del legacy["short_instruction"]
    del legacy["profile"]

    with pytest.raises(VoiceDirectionError, match="VOICE_DIRECTION_INVALID"):
        VoicePerformancePlan.from_dict(legacy)


@pytest.mark.parametrize(
    "payload",
    [
        {"global_speed": 99},
        {"short_instruction": "禁止朗读提示词"},
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
