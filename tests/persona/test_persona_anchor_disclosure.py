from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_gateway import GatewayConfig
from runtime.persona.persona_assembly import UntrustedFragment, assemble_persona
from runtime.persona.persona_loader import load_persona
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime


ROOT = Path(__file__).resolve().parents[2]
RELEASE_PERSONA = ROOT / "linli_character" / "persona_release_v2.json"
NOW = TrustedTime(datetime(2026, 9, 4, tzinfo=timezone.utc))


def _assemble(user_input: str):
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)
    return assemble_persona(
        loaded.snapshot,
        context,
        user_input=user_input,
        max_units=GatewayConfig().max_input_chars,
    )


def _anchor_ids(user_input: str) -> tuple[str, ...]:
    return tuple(
        item_id.removeprefix("declaration.")
        for item_id in _assemble(user_input).budget_report.included_ids
        if item_id.startswith("declaration.anchor.")
    )


@pytest.mark.parametrize(
    "user_input",
    (
        "你今天练琴了吗？",
        "你昨天练琴了吗？",
        "你这几天练琴了吗？",
        "你周末练琴了吗？",
        "你早上练琴了吗？",
        "你今晚还练琴吗？",
        "你练琴了吗？",
        "你最近练琴了吗？",
        "你现在练琴了吗？",
        "你练琴练到几点？",
        "你知道自己最近在练琴吗？",
    ),
)
def test_current_piece_anchor_accepts_direct_questions(user_input: str) -> None:
    assembled = _assemble(user_input)

    assert '"declaration_id":"anchor.current_piece"' in assembled.system_content


def test_cat_anchor_accepts_a_natural_direct_question() -> None:
    assembled = _assemble("你养猫吗？")

    assert '"declaration_id":"anchor.cat"' in assembled.system_content


@pytest.mark.parametrize(
    ("user_input", "expected_anchor"),
    (
        ("你听黑胶吗？", "anchor.listening_shelf"),
        ("你读什么书？", "anchor.reading"),
        ("你喝茶吗？", "anchor.stopping_ritual"),
    ),
)
def test_action_phrasing_selects_the_relevant_anchor(
    user_input: str, expected_anchor: str
) -> None:
    assert expected_anchor in _anchor_ids(user_input)


@pytest.mark.parametrize(
    "user_input",
    ("今天下雨了。", "今天买的面包有点难吃。", "在吗？"),
)
def test_ordinary_letters_receive_exactly_one_anchor(user_input: str) -> None:
    assembled = _assemble(user_input)

    assert assembled.system_content.count('"declaration_id":"anchor.') == 1


@pytest.mark.parametrize(
    "user_input",
    (
        "今天下雨了。",
        "你今天练琴了吗？",
        "你喜欢猫、甜食、黑胶和读书吗？你住在哪里，平时穿什么？",
    ),
)
def test_anchor_disclosure_never_exceeds_the_limit(user_input: str) -> None:
    assert len(_anchor_ids(user_input)) <= 4


@pytest.mark.parametrize(
    "user_input",
    (
        "你有没有发现老师最近在练琴？",
        "你有没有发现爸爸最近在练琴？",
        "你有没有发现小王最近在练琴？",
        "你叫小王最近练琴吗？",
        "你爸爸最近练琴吗？",
        "你老师最近练琴吗？",
        "你妈妈最近练琴吗？",
        "你有没有发现小王9最近在练琴？",
        "你孩子最近练琴吗？",
        "你丈夫最近练琴吗？",
        "你邻居最近练琴吗？",
        "你室友最近练琴吗？",
        "你老板最近练琴吗？",
        "你最近听室友练琴吗？",
        "你听室友弹夜曲吗？",
    ),
)
def test_short_gap_does_not_reassign_other_people_to_persona(user_input: str) -> None:
    anchor_ids = _anchor_ids(user_input)

    assert len(anchor_ids) == 1
    assert "anchor.current_piece" not in anchor_ids


def test_follow_up_fallback_does_not_restore_rejected_history_anchor() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)

    assembled = assemble_persona(
        loaded.snapshot,
        context,
        user_input="怎么回事？",
        history=(UntrustedFragment("history.friend", "你朋友养猫吗？"),),
        max_units=GatewayConfig().max_input_chars,
    )
    anchor_ids = tuple(
        item_id.removeprefix("declaration.")
        for item_id in assembled.budget_report.included_ids
        if item_id.startswith("declaration.anchor.")
    )

    assert len(anchor_ids) == 1
    assert "anchor.cat" not in anchor_ids


def test_baseline_anchor_is_deterministic_for_the_same_letter() -> None:
    assert _anchor_ids("今天下雨了。") == ("anchor.everyday_taste",)


def test_baseline_anchor_rotates_across_different_letters() -> None:
    inputs = tuple(f"普通日常来信第{index}封。" for index in range(8))

    selected = {_anchor_ids(user_input)[0] for user_input in inputs}

    assert len(selected) > 1


def test_anchor_disclosure_fits_default_budget_with_full_history() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)

    assembled = assemble_persona(
        loaded.snapshot,
        context,
        user_input="来" * 2_000,
        history=(UntrustedFragment("history.full", "历" * 3_600),),
        max_units=GatewayConfig().max_input_chars,
    )

    assert assembled.system_content.count('"declaration_id":"anchor.') == 1
    assert assembled.budget_report.dropped_ids == ()
