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


@pytest.mark.parametrize("query", [
    "你能说说外婆的事情吗？", "你的钢琴是怎么来的？", "这架钢琴有什么来历？",
    "你最爱吃什么？", "你家的猫叫什么？", "你平时看些什么书？",
    "生日是哪天？", "你读的是哪所学校？", "我家的钢琴是我父亲买的。",
])
def test_short_character_background_is_available_without_wording_gate(query):
    snapshot = load_persona(RELEASE_PERSONA).snapshot
    result = assemble_persona(snapshot, ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW),
        user_input=query, max_units=GatewayConfig().max_input_chars)
    expected = {d.declaration_id: d.statement for d in snapshot.declarations if d.declaration_id.startswith("anchor.")}
    actual = {block["declaration_id"]: block["statement"] for block in (
        json.loads(raw) for raw in re.findall(r"<community_soft_canon>\s*(.*?)\s*</community_soft_canon>", result.system_content, re.S))}
    assert {k: v for k, v in actual.items() if k.startswith("anchor.")} == expected
    assert result.to_messages()[-1] == {"role": "user", "content": query}
    assert query not in actual.values()
    assert not result.budget_report.dropped_ids


@pytest.mark.parametrize('mode', [ReplyMode.TEXT_LETTER, ReplyMode.SPOKEN_VIDEO, ReplyMode.MUSICAL_VIDEO])
def test_scope_grounding_also_reaches_statements_without_history(mode) -> None:
    assembly = assemble_persona(
        load_persona(RELEASE_PERSONA).snapshot,
        ReplyContext.create(mode, trusted_time=NOW),
        user_input='我们一起听讲座是我编的假设。',
        max_units=GatewayConfig().max_input_chars,
    )
    rules = json.loads(re.search(r'<grounding>\n([^\n]+)\n</grounding>', assembly.system_content)[1])
    # Provider-input contract: scope cannot depend on a recall query or history.
    scope = next(rule for rule in rules if '人物组合' in rule)
    assert '陈述和提问' in scope
    assert '未被说明的个人经历保持未知' in scope
    assert '已知肯定、已知否定和未知' in scope
    assert '“做过”和“没做过”都需要原信依据' in scope
    assert '不把推论说成用户讲过的话' in scope
    assert '资料提到一件物品、作品或人物，不代表其中的内容、原话或具体往事也已知' in scope
    assert '表达自己的当下看法，不给观点虚构出处' in scope
    assert '<untrusted_history>' not in assembly.system_content
    assert assembly.budget_report.dropped_ids == ()


def test_grounding_follows_disclosed_references_once_and_survives_budget_pressure() -> None:
    kwargs = dict(
        user_input='我参加了活动，但没有发言。',
        history=(UntrustedFragment('original', '甲' * 2800),),
        evidence_summaries=(UntrustedFragment('summary', '乙' * 1200),),
    )
    snapshot = load_persona(RELEASE_PERSONA).snapshot
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW)
    full = assemble_persona(snapshot, context, max_units=30000, **kwargs)
    assert full.system_content.rstrip().endswith('</grounding>')
    assert full.system_content.count('陈述和提问都按原文命题核对') == 1
    assert full.system_content.index('</untrusted_history>') < full.system_content.index('<grounding>')
    pressured = assemble_persona(snapshot, context, max_units=full.budget_report.used_units - 4000, **kwargs)
    assert 'reply_grounding' in pressured.budget_report.included_ids
    assert pressured.system_content.count('陈述和提问都按原文命题核对') == 1


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
    attitude = next(rule for rule in rules if '不把猜测的动机' in rule)
    assert '训诫、责备或人格定性' in attitude
    assert '“我猜”' in attitude and '无依据的指责' in attitude
    assert '核对、重复提问、纠正记忆不能证明用户有恶意或心理问题' in attitude
    assert '不同意见和拒绝' in attitude and '主动调侃、嘴硬' in attitude
    assert '轻调侃不等于对真实动机的认定' in attitude
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


def test_release_style_does_not_require_opposition_or_admonishing_closures() -> None:
    assembled = assemble_persona(
        load_persona(RELEASE_PERSONA).snapshot,
        ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=NOW),
        user_input='今天晚饭吃了面，喝了温水。',
        max_units=GatewayConfig().max_input_chars,
    )
    declarations = [json.loads(value) for value in re.findall(
        r'<community_soft_canon>\n([^\n]+)\n</community_soft_canon>', assembled.system_content)]
    statements = {item['declaration_id']: item['statement'] for item in declarations}
    assert '没分歧就自在地聊' in statements['trait.tease_and_refuse']
    assert '用户只分享日常时就聊日常' in statements['style.care_quota']
    assert '不轮换固定套路' in statements['style.vary_closing']
    assert '不替用户补动机' in statements['memory.ask_for_reminder']


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
