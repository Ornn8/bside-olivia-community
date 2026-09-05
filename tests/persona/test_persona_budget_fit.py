import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_gateway import GatewayConfig
from runtime.persona.persona_assembly import UntrustedFragment, assemble_persona
from runtime.persona.persona_loader import load_persona
from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime


ROOT = Path(__file__).resolve().parents[2]
RELEASE_PERSONA = ROOT / "linli_character" / "persona_release_v2.json"
NOW = TrustedTime(datetime(2026, 8, 30, tzinfo=timezone.utc))


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.SPOKEN_VIDEO, ReplyMode.MUSICAL_VIDEO])
def test_release_attitude_requires_evidence_without_removing_personality(mode) -> None:
    loaded = load_persona(RELEASE_PERSONA)
    assembled = assemble_persona(
        loaded.snapshot, ReplyContext.create(mode, trusted_time=NOW),
        user_input='我只是在核对记忆，不是在试探你。',
        max_units=GatewayConfig().max_input_chars,
    )
    rules = json.loads(re.search(r'<forbidden>\n([^\n]+)\n</forbidden>', assembled.system_content)[1])
    # This checks the provider-input contract, not the model's compliance.
    attitude = next(rule for rule in rules if '对用户的态度' in rule)
    assert '明确言行或可核对事实' in attitude
    assert '“我猜”' in attitude and '无依据的指责' in attitude
    assert '核对、重复提问、纠正记忆本身不表示' in attitude
    assert '不同意见和拒绝' in attitude and '明确的玩笑' in attitude
    assert '不顺带给用户的理智、品性或生活选择打分' in attitude
    assert '有分歧或越界就谈具体行为及自己的边界' in attitude
    assert '"declaration_id":"trait.tease_and_refuse"' in assembled.system_content
    assert assembled.user_content == '我只是在核对记忆，不是在试探你。'


def test_release_persona_fits_default_budget() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)
    config = GatewayConfig.from_mapping({})

    assembled = assemble_persona(
        loaded.snapshot,
        context,
        user_input="今天下雨了。",
        max_units=config.max_input_chars,
    )

    assert loaded.snapshot.status == "READY"
    assert GatewayConfig().max_input_chars == 30_000
    assert config.max_input_chars == 30_000
    invalid = GatewayConfig.from_mapping({"max_input_chars": "invalid"})
    assert invalid.max_input_chars == 30_000
    assert assembled.budget_report.dropped_ids == ()


def test_shipped_llm_config_uses_the_public_input_budget() -> None:
    payload = json.loads(
        (ROOT / "contracts" / "llm_config.example.json").read_text(encoding="utf-8")
    )

    configured = GatewayConfig.from_mapping(payload)
    assert configured.max_input_chars == GatewayConfig().max_input_chars


def test_release_persona_fits_with_full_history() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)
    history = (UntrustedFragment("history.full", "历" * 3_600),)

    assembled = assemble_persona(
        loaded.snapshot,
        context,
        user_input="来" * 2_000,
        history=history,
        max_units=GatewayConfig().max_input_chars,
    )

    assert assembled.budget_report.dropped_ids == ()


def test_public_30k_budget_preserves_full_context_that_22k_would_drop() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)
    kwargs = {
        "user_input": "猫、食物、复兴公园、肖邦夜曲" + "问" * 1_800,
        "history": (UntrustedFragment("history.full", "历" * 3_600),),
        "evidence_summaries": (UntrustedFragment("evidence.full", "证" * 3_000),),
    }

    legacy_budget = assemble_persona(
        loaded.snapshot,
        context,
        max_units=22_000,
        **kwargs,
    )
    public_budget = assemble_persona(
        loaded.snapshot,
        context,
        max_units=GatewayConfig().max_input_chars,
        **kwargs,
    )

    assert legacy_budget.budget_report.dropped_ids
    assert public_budget.budget_report.dropped_ids == ()


def test_release_persona_progressively_discloses_relevant_soft_anchors() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)

    ordinary = assemble_persona(
        loaded.snapshot,
        context,
        user_input="今天开会有点累，刚刚才结束。",
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    food = assemble_persona(
        loaded.snapshot,
        context,
        user_input="你平时喜欢吃什么，口味偏甜还是偏辣？",
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    residence = assemble_persona(
        loaded.snapshot,
        context,
        user_input="你现在住在哪里？家里能放下三角钢琴吗？",
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    current_piece = assemble_persona(
        loaded.snapshot,
        context,
        user_input="你最近在练什么曲子？老师在帮你调整什么？",
        max_units=GatewayConfig().max_input_chars,
    ).system_content

    assert '"declaration_id":"style.care_quota"' in ordinary
    assert ordinary.count('"declaration_id":"anchor.') == 1
    assert '"declaration_id":"anchor.everyday_taste"' in food
    assert '"declaration_id":"anchor.cat"' not in food
    assert '"declaration_id":"anchor.residence"' in residence
    assert '"declaration_id":"anchor.physical"' not in residence
    assert '"declaration_id":"anchor.grandmother_piano"' not in residence
    assert '"declaration_id":"anchor.school_timeline"' not in residence
    assert '"declaration_id":"anchor.current_piece"' in current_piece
    assert '"declaration_id":"anchor.listening_shelf"' not in current_piece
    assert '"declaration_id":"anchor.school_timeline"' not in current_piece
    assert food.count('"declaration_id":"anchor.') <= 4
    assert residence.count('"declaration_id":"anchor.') <= 4


def test_progressive_disclosure_uses_recent_history_for_follow_up_letters() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)

    assembled = assemble_persona(
        loaded.snapshot,
        context,
        user_input="那后来呢？",
        history=(UntrustedFragment("history.recent", "你去云南遇到蜘蛛以后呢？"),),
        max_units=GatewayConfig().max_input_chars,
    )

    assert '"declaration_id":"anchor.afraid_of_bugs"' in assembled.system_content
    assert '<constitution>' in assembled.system_content
    assert assembled.system_content.count('"declaration_id":"anchor.') <= 4


def test_progressive_disclosure_rejects_idioms_and_prioritizes_current_letter() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)

    idioms = assemble_persona(
        loaded.snapshot,
        context,
        user_input="我真的撑不住了。这门课程让我吃了个大亏，帮我调音以后我决定住手。",
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    current_topic = assemble_persona(
        loaded.snapshot,
        context,
        user_input="你喜欢猫吗？",
        history=(
            UntrustedFragment("history.food", "你平时喜欢吃什么？"),
            UntrustedFragment("history.home", "你现在住在哪里？"),
        ),
        evidence_summaries=(
            UntrustedFragment("evidence.piano", "最近在练什么曲子？"),
            UntrustedFragment("evidence.school", "你在哪所音乐学院上学？"),
        ),
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    own_topics = assemble_persona(
        loaded.snapshot,
        context,
        user_input="我告诉你，我最近在练琴，也很喜欢猫。",
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    reciprocal = assemble_persona(
        loaded.snapshot,
        context,
        user_input="我喜欢甜食，你呢？",
        max_units=GatewayConfig().max_input_chars,
    ).system_content

    assert idioms.count('"declaration_id":"anchor.') == 1
    assert own_topics.count('"declaration_id":"anchor.') == 1
    assert '"declaration_id":"anchor.cat"' in current_topic
    assert '"declaration_id":"anchor.everyday_taste"' not in current_topic
    assert '"declaration_id":"anchor.residence"' not in current_topic
    assert '"declaration_id":"anchor.current_piece"' not in current_topic
    assert '"declaration_id":"anchor.school_timeline"' not in current_topic
    assert '"declaration_id":"anchor.everyday_taste"' in reciprocal


def test_progressive_disclosure_does_not_treat_user_details_as_persona_details() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)

    comparison = assemble_persona(
        loaded.snapshot,
        context,
        user_input="我和林离一样最近在练琴。",
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    observation = assemble_persona(
        loaded.snapshot,
        context,
        user_input="你有没有发现我最近在练琴？",
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    third_person = assemble_persona(
        loaded.snapshot,
        context,
        user_input="你有没有发现她最近在练琴？",
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    named_others = tuple(
        assemble_persona(
            loaded.snapshot,
            context,
            user_input=user_input,
            max_units=GatewayConfig().max_input_chars,
        ).system_content
        for user_input in (
            "你有没有发现老师最近在练琴？",
            "你有没有发现爸爸最近在练琴？",
            "你有没有发现小王最近在练琴？",
        )
    )
    cross_sentence = assemble_persona(
        loaded.snapshot,
        context,
        user_input="你好吗？最近在练琴好累。",
        max_units=GatewayConfig().max_input_chars,
    ).system_content
    bare_name = assemble_persona(
        loaded.snapshot,
        context,
        user_input="林离这个名字很好听，Olivia 在音乐学院读书。",
        max_units=GatewayConfig().max_input_chars,
    ).system_content

    assert '"declaration_id":"anchor.current_piece"' not in comparison
    assert '"declaration_id":"anchor.current_piece"' not in observation
    assert '"declaration_id":"anchor.current_piece"' not in third_person
    assert all(
        '"declaration_id":"anchor.current_piece"' not in item
        for item in named_others
    )
    assert '"declaration_id":"anchor.current_piece"' not in cross_sentence
    assert bare_name.count('"declaration_id":"anchor.') == 1


def test_current_persona_question_without_known_anchor_does_not_fall_back_to_history() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)

    assembled = assemble_persona(
        loaded.snapshot,
        context,
        user_input="你今天心情怎么样？",
        history=(UntrustedFragment("history.home", "你住在哪里？"),),
        max_units=GatewayConfig().max_input_chars,
    ).system_content

    assert '"declaration_id":"anchor.residence"' not in assembled


def test_soft_canon_outlives_history_under_pressure() -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)
    history = (
        UntrustedFragment("older", "甲" * 1_800),
        UntrustedFragment("recent", "乙" * 1_800),
    )
    full = assemble_persona(
        loaded.snapshot,
        context,
        user_input="今天下雨了。",
        history=history,
        max_units=100_000,
    )

    pressured = assemble_persona(
        loaded.snapshot,
        context,
        user_input="今天下雨了。",
        history=history,
        max_units=full.budget_report.used_units - 4_000,
    )

    dropped = pressured.budget_report.dropped_ids
    history_positions = [i for i, item_id in enumerate(dropped) if item_id.startswith("history.")]
    soft_ids = {
        f"declaration.{item.declaration_id}"
        for item in loaded.snapshot.declarations
        if item.tier == "COMMUNITY_SOFT_CANON"
    }
    soft_positions = [i for i, item_id in enumerate(dropped) if item_id in soft_ids]

    assert history_positions
    assert not soft_positions or min(history_positions) < min(soft_positions)


@pytest.mark.parametrize(
    "mode", (ReplyMode.SPOKEN_VIDEO, ReplyMode.MUSICAL_VIDEO)
)
def test_release_persona_fits_video_mode_budget(mode: ReplyMode) -> None:
    loaded = load_persona(RELEASE_PERSONA)
    context = ReplyContext.create(mode, trusted_time=NOW)

    assembled = assemble_persona(
        loaded.snapshot,
        context,
        user_input="今天下雨了。",
        max_units=GatewayConfig().max_input_chars,
    )

    assert assembled.budget_report.dropped_ids == ()
