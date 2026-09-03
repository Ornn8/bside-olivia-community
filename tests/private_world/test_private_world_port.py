import json
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from private_world_port import (
    ActiveBoundary,
    ContinuationAwareness,
    HomeAccess,
    IntimacyGrant,
    LocalContinuationFact,
    NullPrivateWorldPort,
    PrivateWorldError,
    PrivateWorldSnapshot,
)
from runtime.reply.reply_context import IntimacyTier


ROOT = Path(__file__).resolve().parents[2]


def _private_world_validator(schema: dict[str, object]) -> Draft202012Validator:
    checker = FormatChecker()

    @checker.checks("date-time", raises=(TypeError, ValueError))
    def is_iso_datetime(value: object) -> bool:
        if not isinstance(value, str):
            return True
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True

    return Draft202012Validator(schema, format_checker=checker)


def _grant(number: int, *, grant_id: str | None = None) -> IntimacyGrant:
    return IntimacyGrant(
        grant_id=grant_id or f"grant.{number}",
        tier=IntimacyTier.LIGHT_CONTACT,
        statement=f"Synthetic grant {number}.",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"grant_id": "not allowed", "tier": IntimacyTier.NONE, "statement": "ok"},
        {"grant_id": "grant.valid", "tier": "none", "statement": "ok"},
        {"grant_id": "grant.valid", "tier": IntimacyTier.NONE, "statement": ""},
        {
            "grant_id": "grant.valid",
            "tier": IntimacyTier.NONE,
            "statement": "x" * 201,
        },
        {
            "grant_id": "grant.valid",
            "tier": IntimacyTier.NONE,
            "statement": "bad\nstatement",
        },
    ],
)
def test_intimacy_grant_rejects_invalid_inputs(kwargs: dict[str, object]) -> None:
    with pytest.raises(PrivateWorldError):
        IntimacyGrant(**kwargs)


def test_snapshot_rejects_unbounded_or_duplicate_intimacy_grants() -> None:
    with pytest.raises(PrivateWorldError):
        PrivateWorldSnapshot(intimacy_grants=tuple(_grant(i) for i in range(17)))
    with pytest.raises(PrivateWorldError):
        PrivateWorldSnapshot(
            intimacy_grants=(
                _grant(1, grant_id="grant.duplicate"),
                _grant(2, grant_id="grant.duplicate"),
            )
        )


def _continuations() -> tuple[LocalContinuationFact, ...]:
    return (
        LocalContinuationFact(
            "trip.pending",
            "下个月可能有一段旅行安排。",
            ContinuationAwareness.PENDING,
        ),
        LocalContinuationFact(
            "class.known",
            "她已经知道下周课程时间会调整。",
            ContinuationAwareness.CHARACTER_KNOWN,
        ),
        LocalContinuationFact(
            "plan.control",
            "控制层保存的未来计划。",
            ContinuationAwareness.CONTROL_ONLY,
        ),
    )


def test_snapshot_keeps_hidden_and_control_only_values_out_of_character_view() -> None:
    snapshot = PrivateWorldSnapshot(
        familiarity=72,
        trust=61,
        comfort=55,
        closeness=48,
        tension=13,
        relationship_stage="close",
        nickname_permissions=("小河豚", "旅行者"),
        home_access=HomeAccess.VISIT_ACCESS,
        continuation_facts=_continuations(),
    )

    control = snapshot.control_view().to_dict()
    character = snapshot.character_view().to_dict()

    assert control["trust"] == 61
    assert control["nickname_permissions"] == ["小河豚", "旅行者"]
    assert len(control["continuation_facts"]) == 3
    assert character == {
        "view": "character",
        "relationship_stage": "close",
        "granted_intimacy": "none",
        "nickname_permissions": ["小河豚", "旅行者"],
        "home_history_allowed": True,
        "continuation_known": True,
            "continuation_facts": [
            {
                "fact_id": "class.known",
                "statement": "她已经知道下周课程时间会调整。",
                "awareness": "character_known",
                }
            ],
            "active_boundaries": [],
            "acknowledged_affection": None,
    }
    serialized = repr(character)
    assert "pending" not in serialized
    assert "control_only" not in serialized
    assert "未来计划" not in serialized
    for hidden in ("familiarity", "trust", "comfort", "closeness", "tension"):
        assert hidden not in character


def test_snapshot_exposes_intimacy_control_state_without_leaking_grant_details() -> None:
    snapshot = PrivateWorldSnapshot(
        intimacy_grants=(
            IntimacyGrant(
                "grant.light",
                IntimacyTier.LIGHT_CONTACT,
                "A synthetic light-contact grant.",
            ),
            IntimacyGrant(
                "grant.close",
                IntimacyTier.CLOSE_CONTACT,
                "A synthetic close-contact grant.",
            ),
        ),
        growth_window_start="2026-08-29T00:00:00+00:00",
        growth_used=4,
    )

    control = snapshot.control_view().to_dict()
    character = snapshot.character_view().to_dict()

    assert control["intimacy_grants"] == [
        {
            "grant_id": "grant.light",
            "tier": "light_contact",
            "statement": "A synthetic light-contact grant.",
        },
        {
            "grant_id": "grant.close",
            "tier": "close_contact",
            "statement": "A synthetic close-contact grant.",
        },
    ]
    assert control["growth_window_start"] == "2026-08-29T00:00:00+00:00"
    assert control["growth_used"] == 4
    assert character["granted_intimacy"] == "close_contact"
    assert set(character) == {
        "view",
        "relationship_stage",
        "granted_intimacy",
        "nickname_permissions",
        "home_history_allowed",
        "continuation_known",
        "continuation_facts",
        "active_boundaries",
        "acknowledged_affection",
    }
    assert "statement" not in repr(character)
    assert "growth_" not in repr(character)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"growth_window_start": "not-a-time"},
        {"growth_window_start": "2026-08-29T00:00:00"},
        {"growth_window_start": "2026-08-29T09:00:00+09:00"},
        {"growth_window_start": "2026-08-29T00:00:00-00:00"},
        {"growth_window_start": "2026-08-29T00:00:00+0000"},
        {"growth_used": -1},
        {"growth_used": 256},
        {"growth_used": True},
    ],
)
def test_snapshot_rejects_invalid_growth_window_state(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(PrivateWorldError):
        PrivateWorldSnapshot(**kwargs)


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        "not-a-timestamp+00:00",
        "2026-02-31T00:00:00+00:00",
    ],
)
def test_schema_rejects_invalid_utc_timestamps(
    invalid_timestamp: str,
) -> None:
    schema = json.loads(
        (ROOT / "contracts" / "private_world.schema.json").read_text(
            encoding="utf-8"
        )
    )
    payload = PrivateWorldSnapshot().to_dict()
    payload["growth_window_start"] = invalid_timestamp

    validator = _private_world_validator(schema)
    assert list(validator.iter_errors(payload))


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        "2026-08-29T09:00:00+09:00",
        "2026-08-29T00:00:00",
    ],
)
def test_boundary_timestamp_is_utc_in_runtime_and_schema(
    invalid_timestamp: str,
) -> None:
    schema = json.loads(
        (ROOT / "contracts" / "private_world.schema.json").read_text(
            encoding="utf-8"
        )
    )
    payload = PrivateWorldSnapshot().to_dict()
    payload["active_boundaries"] = [
        {
            "boundary_id": "boundary.synthetic",
            "set_at": invalid_timestamp,
            "scope": "synthetic scope",
        }
    ]

    with pytest.raises(PrivateWorldError):
        ActiveBoundary("boundary.synthetic", invalid_timestamp, "synthetic scope")
    assert list(_private_world_validator(schema).iter_errors(payload))


def test_boundary_scope_character_rules_match_runtime_and_schema() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "private_world.schema.json").read_text(
            encoding="utf-8"
        )
    )
    payload = PrivateWorldSnapshot().to_dict()
    payload["active_boundaries"] = [
        {
            "boundary_id": "boundary.synthetic",
            "set_at": "2026-08-29T00:00:00+00:00",
            "scope": "bad\nscope",
        }
    ]

    with pytest.raises(PrivateWorldError):
        ActiveBoundary(
            "boundary.synthetic",
            "2026-08-29T00:00:00+00:00",
            "bad\nscope",
        )
    assert list(_private_world_validator(schema).iter_errors(payload))


def test_contract_rejects_unbounded_or_ambiguous_values() -> None:
    with pytest.raises(PrivateWorldError):
        PrivateWorldSnapshot(trust=101)
    with pytest.raises(PrivateWorldError):
        PrivateWorldSnapshot(relationship_stage="x" * 65)
    with pytest.raises(PrivateWorldError):
        PrivateWorldSnapshot(nickname_permissions=("not allowed whitespace",))
    with pytest.raises(PrivateWorldError):
        PrivateWorldSnapshot(nickname_permissions=("重复", "重复"))
    with pytest.raises(PrivateWorldError):
        PrivateWorldSnapshot(home_access="visit_access")  # type: ignore[arg-type]
    with pytest.raises(PrivateWorldError):
        LocalContinuationFact("not allowed", "fact")
    with pytest.raises(PrivateWorldError):
        LocalContinuationFact("fact", "bad\nstatement")


def test_python_and_schema_agree_on_unicode_nicknames_and_continuation_views() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "private_world.schema.json").read_text(encoding="utf-8")
    )
    validator = _private_world_validator(schema)
    snapshot = PrivateWorldSnapshot(
        relationship_stage="x" * 64,
        nickname_permissions=("小河豚",),
        home_access=HomeAccess.DOMESTIC_ACCESS,
        continuation_facts=_continuations(),
    )

    assert list(validator.iter_errors(snapshot.to_dict())) == []
    assert list(validator.iter_errors(snapshot.control_view().to_dict())) == []
    assert list(validator.iter_errors(snapshot.character_view().to_dict())) == []

    unsafe = snapshot.character_view().to_dict()
    unsafe["continuation_facts"].append(
        {
            "fact_id": "unsafe.pending",
            "statement": "角色还不应该知道。",
            "awareness": "pending",
        }
    )
    assert list(validator.iter_errors(unsafe))

    with pytest.raises(PrivateWorldError):
        PrivateWorldSnapshot(relationship_stage="x" * 65)


def test_null_port_is_stable_and_has_no_mutation_surface() -> None:
    port = NullPrivateWorldPort()

    assert port.snapshot() == PrivateWorldSnapshot()
    assert port.control_view() == PrivateWorldSnapshot().control_view()
    assert port.character_view() == PrivateWorldSnapshot().character_view()
    assert not hasattr(port, "append")
    assert not hasattr(port, "commit")
    assert not hasattr(port, "store")


def test_public_documentation_keeps_persistence_and_projection_out_of_scope() -> None:
    documentation = (ROOT / "docs" / "P02_10_PRIVATE_WORLD.md").read_text(
        encoding="utf-8"
    )

    for marker in (
        "PrivateWorldPort",
        "NullPrivateWorldPort",
        "control view",
        "character view",
        "LocalContinuationFact",
        "does not persist",
        "does not call a provider",
        "P02-11",
        "P02-13",
    ):
        assert marker in documentation
