import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    LocalContinuationFact,
    NullPrivateWorldPort,
    PrivateWorldError,
    PrivateWorldSnapshot,
)


ROOT = Path(__file__).resolve().parents[2]


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
    }
    serialized = repr(character)
    assert "pending" not in serialized
    assert "control_only" not in serialized
    assert "未来计划" not in serialized
    for hidden in ("familiarity", "trust", "comfort", "closeness", "tension"):
        assert hidden not in character


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
    validator = Draft202012Validator(schema)
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
