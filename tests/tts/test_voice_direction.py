import asyncio

import pytest

from persona_loader import PersonaDeclaration, PersonaProfile, PersonaSnapshot
from voice_direction import (
    VoiceDirectionError,
    VoicePerformancePlan,
    VoiceToolCall,
    direct_music_voice_performance,
    direct_voice_performance,
)


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


def _valid_music_direction() -> dict[str, object]:
    return {
        "overall_emotion": "restrained empathy becoming reassurance",
        "global_speed": 1.06,
        "energy": 0.55,
        "breath_before_sentences": [2],
        "emphasize_sentences": [1],
    }


def _persona_snapshot(*, mode: str, statement: str) -> PersonaSnapshot:
    return PersonaSnapshot(
        schema_version="p02.persona.v2",
        persona_id="synthetic.persona",
        declarations=(
            PersonaDeclaration(
                declaration_id=f"mode.{mode}.synthetic",
                source_id="source.synthetic",
                tier="MODE_STYLE",
                confidence="HIGH",
                rights_status="SUMMARY_ONLY",
                allowed_public_release=True,
                statement=statement,
                mode=mode,
                facet="MODE_STYLE",
            ),
        ),
        status="READY",
        source="persona_v2",
        profile=PersonaProfile(
            display_name="Synthetic",
            locale="zh-CN",
            summary="Synthetic persona.",
            required_facets=("MODE_STYLE",),
            required_modes=(mode,),
        ),
    )


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


def test_voice_director_injects_only_the_spoken_mode_persona_direction() -> None:
    statement = "Synthetic spoken persona direction marker."
    director = _FakeVoiceDirector(_valid_direction())

    plan = asyncio.run(
        direct_voice_performance(
            "第一句。第二句。",
            director,
            persona_snapshot=_persona_snapshot(
                mode="spoken_video",
                statement=statement,
            ),
        )
    )

    request = director.requests[0]
    tool_description = request["tools"][0]["function"]["description"]
    assert statement in tool_description
    assert plan.persona_projection_status == "READY"
    restored = VoicePerformancePlan.from_dict(plan.to_dict())
    assert restored.persona_projection_status == "READY"
    assert restored == plan


def test_director_rejects_tool_calls_missing_required_controls() -> None:
    arguments = _valid_direction()
    del arguments["short_instruction"]

    with pytest.raises(VoiceDirectionError, match="VOICE_DIRECTION_INVALID"):
        asyncio.run(direct_voice_performance("第一句。第二句。", _FakeVoiceDirector(arguments)))


def test_music_director_preserves_the_pre_a_tool_and_plan_profile() -> None:
    reply = "第一句。第二句。"
    director = _FakeVoiceDirector(_valid_music_direction())

    plan = asyncio.run(direct_music_voice_performance(reply, director))

    assert plan.spoken_text == reply
    assert plan.profile == "legacy_music_global_direction_v1"
    assert plan.short_instruction == ""
    assert plan.global_speed == 1.06
    properties = director.requests[0]["tools"][0]["function"]["parameters"][
        "properties"
    ]
    assert set(properties) == {
        "overall_emotion",
        "global_speed",
        "energy",
        "breath_before_sentences",
        "emphasize_sentences",
    }
    legacy = plan.to_dict()
    assert "short_instruction" not in legacy and "profile" not in legacy
    assert VoicePerformancePlan.from_music_dict(legacy) == plan
    with pytest.raises(VoiceDirectionError, match="VOICE_DIRECTION_INVALID"):
        VoicePerformancePlan.from_dict(plan.to_dict())


def test_music_director_exposes_mode_style_fallback_instead_of_hiding_it() -> None:
    statement = "Synthetic letter-only persona direction marker."
    director = _FakeVoiceDirector(_valid_music_direction())

    plan = asyncio.run(
        direct_music_voice_performance(
            "第一句。第二句。",
            director,
            persona_snapshot=_persona_snapshot(
                mode="text_letter",
                statement=statement,
            ),
        )
    )

    tool_description = director.requests[0]["tools"][0]["function"]["description"]
    assert "projection_status=FALLBACK_MODE_STYLE_EMPTY" in tool_description
    assert statement not in tool_description
    assert plan.persona_projection_status == "FALLBACK_MODE_STYLE_EMPTY"


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
