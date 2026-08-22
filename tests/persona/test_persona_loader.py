from __future__ import annotations

import json
from pathlib import Path

from persona_loader import (
    PersonaDeclaration,
    PersonaLoadErrorCode,
    PersonaSnapshot,
    load_persona,
)


ROOT = Path(__file__).resolve().parents[2]


def _valid_registry() -> dict[str, object]:
    return {
        "schema_version": "p02.persona.v2",
        "persona_id": "synthetic.persona",
        "declarations": [
            {
                "declaration_id": "declaration.synthetic",
                "source_id": "source.synthetic",
                "tier": "CONSTITUTION",
                "confidence": "HIGH",
                "rights_status": "REDISTRIBUTABLE",
                "allowed_public_release": True,
                "statement": "Synthetic contract rule.",
            }
        ],
    }


def test_valid_registry_returns_a_typed_persona_snapshot(tmp_path: Path) -> None:
    config_path = tmp_path / "persona.json"
    config_path.write_text(json.dumps(_valid_registry()), encoding="utf-8")

    result = load_persona(config_path)

    assert result.error_code is None
    assert result.fallback is False
    assert isinstance(result.snapshot, PersonaSnapshot)
    assert result.snapshot.status == "READY"
    assert result.snapshot.source == "persona_v2"
    assert result.snapshot.persona_id == "synthetic.persona"
    assert result.snapshot.declarations == (
        PersonaDeclaration(
            declaration_id="declaration.synthetic",
            source_id="source.synthetic",
            tier="CONSTITUTION",
            confidence="HIGH",
            rights_status="REDISTRIBUTABLE",
            allowed_public_release=True,
            statement="Synthetic contract rule.",
            mode=None,
        ),
    )


def test_missing_registry_returns_an_empty_sanitized_draft(tmp_path: Path) -> None:
    missing_path = tmp_path / "private-user-folder" / "persona.json"

    result = load_persona(missing_path)

    assert result.error_code == PersonaLoadErrorCode.FILE_MISSING
    assert result.fallback is True
    assert result.snapshot.status == "DRAFT"
    assert result.snapshot.source == "draft"
    assert result.snapshot.persona_id is None
    assert result.snapshot.declarations == ()
    assert str(missing_path) not in repr(result)


def test_invalid_registry_inputs_fail_closed_with_stable_codes(tmp_path: Path) -> None:
    schema_path = ROOT / "contracts" / "persona_v2.schema.json"

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"schema_version":', encoding="utf-8")
    malformed_result = load_persona(malformed, schema_path=schema_path)

    unreadable = tmp_path / "unreadable.json"
    unreadable.write_bytes(b"\xff\xfe\x00")
    unreadable_result = load_persona(unreadable, schema_path=schema_path)

    schema_invalid = tmp_path / "schema-invalid.json"
    invalid_registry = _valid_registry()
    del invalid_registry["declarations"]
    schema_invalid.write_text(json.dumps(invalid_registry), encoding="utf-8")
    schema_invalid_result = load_persona(schema_invalid, schema_path=schema_path)

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(_valid_registry()), encoding="utf-8")
    missing_schema = tmp_path / "missing-schema.json"
    unavailable_schema_result = load_persona(valid, schema_path=missing_schema)

    assert malformed_result.error_code == PersonaLoadErrorCode.JSON_INVALID
    assert unreadable_result.error_code == PersonaLoadErrorCode.READ_FAILED
    assert schema_invalid_result.error_code == PersonaLoadErrorCode.SCHEMA_INVALID
    assert unavailable_schema_result.error_code == PersonaLoadErrorCode.SCHEMA_UNAVAILABLE
    for result in (
        malformed_result,
        unreadable_result,
        schema_invalid_result,
        unavailable_schema_result,
    ):
        assert result.fallback is True
        assert result.snapshot.declarations == ()
        assert str(tmp_path) not in repr(result)


def test_release_rights_allow_summary_but_block_private_declarations(tmp_path: Path) -> None:
    schema_path = ROOT / "contracts" / "persona_v2.schema.json"

    summary_path = tmp_path / "summary.json"
    summary_registry = _valid_registry()
    summary_registry["declarations"][0]["rights_status"] = "SUMMARY_ONLY"
    summary_path.write_text(json.dumps(summary_registry), encoding="utf-8")

    private_path = tmp_path / "private.json"
    private_registry = _valid_registry()
    private_registry["declarations"][0]["rights_status"] = "LOCAL_PRIVATE_ONLY"
    private_registry["declarations"][0]["allowed_public_release"] = False
    private_path.write_text(json.dumps(private_registry), encoding="utf-8")

    summary_result = load_persona(summary_path, schema_path=schema_path)
    private_result = load_persona(private_path, schema_path=schema_path)

    assert summary_result.fallback is False
    assert summary_result.snapshot.declarations[0].rights_status == "SUMMARY_ONLY"
    assert private_result.fallback is True
    assert private_result.error_code == PersonaLoadErrorCode.RIGHTS_BLOCKED
    assert private_result.snapshot.declarations == ()
