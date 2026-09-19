from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts" / "persona_v2.schema.json"


def test_persona_v2_schema_has_a_stable_contract_identity() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "p02.persona.v2"
    assert schema["properties"]["schema_version"]["const"] == "p02.persona.v2"


def test_each_declaration_requires_source_classification_metadata() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    valid = {
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

    assert list(validator.iter_errors(valid)) == []
    declaration = valid["declarations"][0]
    for field in (
        "source_id",
        "tier",
        "confidence",
        "rights_status",
        "allowed_public_release",
    ):
        invalid = json.loads(json.dumps(valid))
        del invalid["declarations"][0][field]
        assert list(validator.iter_errors(invalid)), field


def test_each_declaration_requires_a_stable_id_and_non_empty_statement() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    valid = {
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

    assert list(validator.iter_errors(valid)) == []
    for field in ("declaration_id", "statement"):
        invalid = json.loads(json.dumps(valid))
        del invalid["declarations"][0][field]
        assert list(validator.iter_errors(invalid)), field

    empty_statement = json.loads(json.dumps(valid))
    empty_statement["declarations"][0]["statement"] = ""
    assert list(validator.iter_errors(empty_statement))

    oversized_statement = json.loads(json.dumps(valid))
    oversized_statement["declarations"][0]["statement"] = "x" * 601
    assert list(validator.iter_errors(oversized_statement))


def test_unknown_or_local_private_rights_fail_closed_for_public_release() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for rights_status in ("UNKNOWN_BLOCK_RELEASE", "LOCAL_PRIVATE_ONLY"):
        declaration = {
            "declaration_id": "declaration.restricted",
            "source_id": "source.restricted",
            "tier": "PUBLIC_CANON",
            "confidence": "HIGH",
            "rights_status": rights_status,
            "allowed_public_release": True,
            "statement": "Synthetic restricted declaration.",
        }
        invalid = {
            "schema_version": "p02.persona.v2",
            "persona_id": "synthetic.persona",
            "declarations": [declaration],
        }
        assert list(validator.iter_errors(invalid)), rights_status

        declaration["allowed_public_release"] = False
        assert list(validator.iter_errors(invalid)) == []


def test_mode_style_requires_a_supported_communication_mode() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for mode in ("text_letter", "spoken_video", "musical_video", "future_im"):
        valid = {
            "schema_version": "p02.persona.v2",
            "persona_id": "synthetic.persona",
            "declarations": [
                {
                    "declaration_id": "declaration.style",
                    "source_id": "source.style",
                    "tier": "MODE_STYLE",
                    "confidence": "HIGH",
                    "rights_status": "REDISTRIBUTABLE",
                    "allowed_public_release": True,
                    "statement": "Synthetic mode style.",
                    "mode": mode,
                }
            ],
        }
        assert list(validator.iter_errors(valid)) == [], mode

    missing_mode = {
        "schema_version": "p02.persona.v2",
        "persona_id": "synthetic.persona",
        "declarations": [
            {
                "declaration_id": "declaration.style",
                "source_id": "source.style",
                "tier": "MODE_STYLE",
                "confidence": "HIGH",
                "rights_status": "REDISTRIBUTABLE",
                "allowed_public_release": True,
                "statement": "Synthetic mode style.",
            }
        ],
    }
    assert list(validator.iter_errors(missing_mode))

    unsupported_mode = json.loads(json.dumps(missing_mode))
    unsupported_mode["declarations"][0]["mode"] = "chat"
    assert list(validator.iter_errors(unsupported_mode))


def test_mode_is_only_allowed_for_mode_style_declarations() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    declaration = {
        "declaration_id": "declaration.canon",
        "source_id": "source.canon",
        "tier": "PUBLIC_CANON",
        "confidence": "HIGH",
        "rights_status": "REDISTRIBUTABLE",
        "allowed_public_release": True,
        "statement": "Synthetic public canon.",
        "mode": "text_letter",
    }
    invalid = {
        "schema_version": "p02.persona.v2",
        "persona_id": "synthetic.persona",
        "declarations": [declaration],
    }
    assert list(validator.iter_errors(invalid))

    declaration.pop("mode")
    assert list(validator.iter_errors(invalid)) == []


def test_schema_self_check_and_tier_coverage_accept_synthetic_registry() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    expected_tiers = {
        "CONSTITUTION",
        "PUBLIC_CANON",
        "COMMUNITY_SOFT_CANON",
        "INFERRED",
        "UNCERTAINTY",
        "MODE_STYLE",
    }
    assert set(schema["$defs"]["declaration"]["properties"]["tier"]["enum"]) == expected_tiers

    declarations = []
    for index, tier in enumerate(sorted(expected_tiers)):
        declaration = {
            "declaration_id": f"declaration.synthetic.{index}",
            "source_id": f"source.synthetic.{index}",
            "tier": tier,
            "confidence": "MEDIUM",
            "rights_status": "REDISTRIBUTABLE",
            "allowed_public_release": True,
            "statement": "Synthetic tier declaration.",
        }
        if tier == "MODE_STYLE":
            declaration["mode"] = "text_letter"
        declarations.append(declaration)

    registry = {
        "schema_version": "p02.persona.v2",
        "persona_id": "synthetic.persona",
        "declarations": declarations,
    }
    assert list(Draft202012Validator(schema).iter_errors(registry)) == []
