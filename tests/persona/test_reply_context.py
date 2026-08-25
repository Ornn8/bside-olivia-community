import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from reply_context import (
    ReplyContext,
    ReplyContextError,
    BehaviorLevel,
    PrivateBehaviorView,
    OutputChannel,
    OutputConstraints,
    ReplyMode,
    ReplyModeAdapter,
    TrustedTime,
    TrustedWorldFact,
    UnsupportedReplyMode,
)


ROOT = Path(__file__).resolve().parents[2]


def test_text_wire_mode_builds_an_immutable_letter_context() -> None:
    mode = ReplyModeAdapter().from_wire("text")
    context = ReplyContext.create(
        mode,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    )

    assert mode is ReplyMode.TEXT_LETTER
    assert context.to_dict()["wire_mode"] == "text"
    assert context.to_dict()["output_constraints"]["channel"] == "letter"
    assert context.output_constraints.max_characters == 1200


def test_trusted_time_normalizes_to_utc_and_rejects_unstable_inputs() -> None:
    trusted_time = TrustedTime(
        datetime(2026, 8, 22, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    )

    assert trusted_time.instant == datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ReplyContextError):
        TrustedTime(datetime(2026, 8, 22, 0, 0))
    with pytest.raises(ReplyContextError):
        TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc), source="private clock")


def test_spoken_modes_reject_stage_directions_and_control_markup() -> None:
    with pytest.raises(ReplyContextError):
        OutputConstraints(
            channel=OutputChannel.SPOKEN_TEXT,
            allow_stage_directions=True,
        )
    with pytest.raises(ReplyContextError):
        OutputConstraints(
            channel=OutputChannel.SPOKEN_TEXT,
            allow_control_markup=True,
        )


def test_context_exposes_only_identified_facts_and_bounded_behavior_hints() -> None:
    context = ReplyContext.create(
        ReplyMode.TEXT_LETTER,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
        world_facts=(
            TrustedWorldFact(
                fact_id="fact.synthetic",
                source_id="source.synthetic",
                statement="A synthetic public fact.",
            ),
        ),
        private_behavior=PrivateBehaviorView(trust=BehaviorLevel.LOW),
    )

    payload = context.to_dict()
    assert payload["world_facts"][0]["source_id"] == "source.synthetic"
    assert payload["private_behavior"]["trust"] == "low"
    assert "raw_score" not in payload["private_behavior"]


def test_legacy_video_mapping_is_preserved_and_future_im_requires_opt_in() -> None:
    adapter = ReplyModeAdapter()
    trusted_time = TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc))

    assert adapter.from_wire("video") is ReplyMode.SPOKEN_VIDEO
    assert adapter.to_wire(ReplyMode.MUSICAL_VIDEO) == "video"
    with pytest.raises(UnsupportedReplyMode):
        adapter.from_wire("future_im")
    with pytest.raises(UnsupportedReplyMode):
        ReplyContext.create(ReplyMode.FUTURE_IM, trusted_time=trusted_time)

    context = ReplyContext.create(
        ReplyMode.FUTURE_IM,
        trusted_time=trusted_time,
        future_im_enabled=True,
    )
    assert context.to_dict()["wire_mode"] is None
    assert context.to_dict()["output_constraints"]["channel"] == "instant_message"
    assert context.output_constraints.max_characters == 12000


def test_schema_matches_runtime_mode_and_privacy_invariants() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "reply_context.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    valid = ReplyContext.create(
        ReplyMode.MUSICAL_VIDEO,
        trusted_time=TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc)),
    ).to_dict()

    assert list(validator.iter_errors(valid)) == []

    invalid_channel = {**valid, "output_constraints": {**valid["output_constraints"]}}
    invalid_channel["output_constraints"]["channel"] = "letter"
    assert list(validator.iter_errors(invalid_channel))

    invalid_stage = {**valid, "output_constraints": {**valid["output_constraints"]}}
    invalid_stage["output_constraints"]["allow_stage_directions"] = True
    assert list(validator.iter_errors(invalid_stage))

    invalid_private = {**valid, "private_behavior": {**valid["private_behavior"]}}
    invalid_private["private_behavior"]["raw_score"] = 0.9
    assert list(validator.iter_errors(invalid_private))

    invalid_home_access = {
        **valid,
        "private_behavior": {**valid["private_behavior"]},
    }
    invalid_home_access["private_behavior"].pop("home_history_allowed")
    invalid_home_access["private_behavior"]["home_access"] = "visit_access"
    assert list(validator.iter_errors(invalid_home_access))


def test_public_contract_documentation_names_api_errors_and_scope_boundary() -> None:
    documentation = (ROOT / "docs" / "P02_REPLY_CONTEXT.md").read_text(
        encoding="utf-8"
    )

    for marker in (
        "ReplyContext",
        "ReplyModeAdapter",
        "REPLY_CONTEXT_INVALID",
        "REPLY_MODE_UNSUPPORTED",
        "p02.reply-context.v1",
        "future_im",
        "does not call a provider",
    ):
        assert marker in documentation


def test_direct_construction_cannot_bypass_context_invariants() -> None:
    trusted_time = TrustedTime(datetime(2026, 8, 22, tzinfo=timezone.utc))

    with pytest.raises(UnsupportedReplyMode):
        ReplyContext(
            mode=ReplyMode.FUTURE_IM,
            trusted_time=trusted_time,
            output_constraints=OutputConstraints.for_mode(ReplyMode.FUTURE_IM),
        )
    with pytest.raises(ReplyContextError):
        ReplyContext(
            mode=ReplyMode.TEXT_LETTER,
            trusted_time=trusted_time,
            output_constraints=OutputConstraints(OutputChannel.SPOKEN_TEXT),
        )
