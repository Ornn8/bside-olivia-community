import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    NullPrivateWorldPort,
    PrivateWorldError,
    PrivateWorldSnapshot,
)


ROOT = Path(__file__).resolve().parents[2]


def test_snapshot_keeps_hidden_values_out_of_character_view() -> None:
    snapshot = PrivateWorldSnapshot(
        familiarity=72,
        trust=61,
        comfort=55,
        closeness=48,
        tension=13,
        relationship_stage="trusted_friend",
        nickname_permissions=("linli",),
        home_access=HomeAccess.VISIT_ACCESS,
        continuation_awareness=ContinuationAwareness.CHARACTER_KNOWN,
    )

    control = snapshot.control_view().to_dict()
    character = snapshot.character_view().to_dict()

    assert control["trust"] == 61
    assert character == {
        "view": "character",
        "relationship_stage": "trusted_friend",
        "nickname_permissions": ["linli"],
        "home_access": "visit_access",
        "continuation_awareness": "character_known",
    }
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
        PrivateWorldSnapshot(home_access="visit_access")  # type: ignore[arg-type]


def test_python_and_schema_agree_on_relationship_stage_boundary() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "private_world.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    accepted = PrivateWorldSnapshot(relationship_stage="x" * 64).to_dict()

    assert list(validator.iter_errors(accepted)) == []
    assert list(
        validator.iter_errors(PrivateWorldSnapshot().control_view().to_dict())
    ) == []
    assert list(
        validator.iter_errors(PrivateWorldSnapshot().character_view().to_dict())
    ) == []
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
        "does not persist",
        "does not call a provider",
        "P02-11",
        "P02-13",
    ):
        assert marker in documentation
