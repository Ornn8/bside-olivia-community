from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_gateway import GatewayConfig
from runtime.persona.persona_assembly import assemble_persona
from runtime.persona.persona_loader import load_persona
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime


ROOT = Path(__file__).resolve().parents[2]
RELEASE_PERSONA = ROOT / "linli_character" / "persona_release_v2.json"
SITUATIONS = (
    "brief_greeting",
    "ordinary_smalltalk",
    "emotional_acknowledgement",
    "boundary_refusal",
    "natural_close",
    "music_request",
)


def _assembled_content(user_input: str) -> str:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 30, tzinfo=timezone.utc)),
    )
    assembled = assemble_persona(
        loaded.snapshot,
        context,
        user_input=user_input,
        max_units=GatewayConfig().max_input_chars,
    )
    return assembled.system_content


def _selected_situations(user_input: str) -> set[str]:
    content = _assembled_content(user_input)
    return {
        situation
        for situation in SITUATIONS
        if f'"situation":"{situation}"' in content
    }


@pytest.mark.parametrize(
    "user_input",
    (
        "我今天有点难受。",
        "听到这件事我很伤心。",
        "我真的快崩溃了。",
        "最近总有点想哭。",
        "这周压力特别大。",
        "晚上总觉得很孤独。",
        "我最近一直失眠。",
        "今天有点不开心。",
        "我感觉快撑不住了。",
        "这几天什么都提不起劲。",
        "最近觉得什么都没意思。",
        "我今天心情不好。",
    ),
)
def test_common_emotional_language_selects_acknowledgement(user_input: str) -> None:
    assert "emotional_acknowledgement" in _selected_situations(user_input)


@pytest.mark.parametrize(
    "user_input",
    (
        "你必须知道我今天多开心。",
        "我一定要告诉你这件事。",
        "今天必须早点睡。",
    ),
)
def test_declarative_obligation_does_not_select_boundary_refusal(
    user_input: str,
) -> None:
    assert "boundary_refusal" not in _selected_situations(user_input)


@pytest.mark.parametrize(
    "user_input",
    (
        "周末陪我去，不许拒绝。",
        "你必须陪我去。",
    ),
)
def test_imposed_request_selects_boundary_refusal(user_input: str) -> None:
    assert "boundary_refusal" in _selected_situations(user_input)


@pytest.mark.parametrize(
    ("situation", "user_input"),
    (
        ("brief_greeting", "你好！"),
        ("ordinary_smalltalk", "今天路上看到一只猫。"),
        ("emotional_acknowledgement", "我今天很难受。"),
        ("boundary_refusal", "周末陪我去，不许拒绝。"),
        ("natural_close", "我先去忙了。"),
        ("music_request", "能给我弹一首吗？"),
    ),
)
def test_every_release_style_situation_is_reachable(
    situation: str,
    user_input: str,
) -> None:
    assert situation in _selected_situations(user_input)


def test_unmatched_input_falls_back_to_ordinary_smalltalk() -> None:
    assert _selected_situations("今天路上看到一只猫。") == {
        "ordinary_smalltalk"
    }


def test_emotional_acknowledgement_precedes_boundary_refusal() -> None:
    content = _assembled_content("我今天很难受，你必须陪我去。")

    assert content.index('"situation":"emotional_acknowledgement"') < content.index(
        '"situation":"boundary_refusal"'
    )
