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
    assert GatewayConfig().max_input_chars == 22_000
    assert config.max_input_chars == 22_000
    invalid = GatewayConfig.from_mapping({"max_input_chars": "invalid"})
    assert invalid.max_input_chars == 22_000
    assert assembled.budget_report.dropped_ids == ()


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
