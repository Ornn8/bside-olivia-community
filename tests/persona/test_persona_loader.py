from __future__ import annotations

import json
from pathlib import Path

from persona_loader import (
    PersonaDeclaration,
    PersonaLoadErrorCode,
    PersonaProfile,
    PersonaSnapshot,
    PersonaStyleExemplar,
    load_persona,
)


ROOT = Path(__file__).resolve().parents[2]


def _declaration(
    declaration_id: str,
    *,
    tier: str,
    facet: str,
    mode: str | None = None,
    rights_status: str = "SUMMARY_ONLY",
    allowed_public_release: bool = True,
) -> dict[str, object]:
    row: dict[str, object] = {
        "declaration_id": declaration_id,
        "source_id": "source.synthetic",
        "tier": tier,
        "facet": facet,
        "confidence": "HIGH",
        "rights_status": rights_status,
        "allowed_public_release": allowed_public_release,
        "statement": "Synthetic contract rule.",
    }
    if mode is not None:
        row["mode"] = mode
    return row


def _valid_registry() -> dict[str, object]:
    required_facets = [
        "IDENTITY",
        "BACKGROUND",
        "CORE_TRAIT",
        "AUTONOMY",
        "KNOWLEDGE_BOUNDARY",
        "EXPRESSION_STYLE",
        "RELATIONSHIP_STYLE",
        "MEMORY_CONTINUITY",
        "UNCERTAINTY",
    ]
    declarations = [
        _declaration("identity.synthetic", tier="PUBLIC_CANON", facet="IDENTITY"),
        *(
            _declaration(
                f"facet.synthetic.{facet.lower()}",
                tier="CONSTITUTION",
                facet=facet,
            )
            for facet in required_facets
            if facet != "IDENTITY"
        ),
        _declaration(
            "mode.synthetic.text",
            tier="MODE_STYLE",
            facet="MODE_STYLE",
            mode="text_letter",
        ),
        _declaration(
            "mode.synthetic.spoken",
            tier="MODE_STYLE",
            facet="MODE_STYLE",
            mode="spoken_video",
        ),
        _declaration(
            "mode.synthetic.musical",
            tier="MODE_STYLE",
            facet="MODE_STYLE",
            mode="musical_video",
        ),
    ]
    return {
        "schema_version": "p02.persona.v2",
        "persona_id": "synthetic.persona",
        "profile": {
            "display_name": "Synthetic Character",
            "locale": "en-US",
            "summary": "A complete synthetic character profile.",
            "required_facets": required_facets,
            "required_modes": [
                "text_letter",
                "spoken_video",
                "musical_video",
            ],
        },
        "declarations": declarations,
    }


def _registry_with_style_exemplar() -> dict[str, object]:
    payload = _valid_registry()
    payload["style_exemplars"] = [
        {
            "exemplar_id": "style.synthetic.greeting",
            "source_id": "source.synthetic",
            "derivation": "SYNTHETIC",
            "rights_status": "REDISTRIBUTABLE",
            "allowed_public_release": True,
            "mode": "text_letter",
            "situation": "brief_greeting",
            "user_text": "A synthetic greeting.",
            "assistant_text": "A short, character-specific reply.",
            "style_only": True,
            "factual_authority": False,
            "user_text_is_synthetic": True,
            "assistant_text_is_verbatim": False,
        }
    ]
    return payload


def _style_provenance(source_id: str = "source.synthetic") -> dict[str, object]:
    return {
        "source_id": source_id, "user_folder_count": 30, "fold_count": 5,
        "fold_user_counts": [6, 6, 6, 6, 6],
        "holdout_fold": 0, "training_folds": [1, 2, 3, 4],
        "training_text_count": 120,
        "videos_excluded": True,
        "user_folders_indivisible": True,
        "assignment_method": "NFC_SHA256_SORT_ROUND_ROBIN",
        "holdout_body_read": False,
        "user_text_policy": "SYNTHETIC",
        "assistant_text_policy": "NON_VERBATIM_ABSTRACTION",
        "contiguous_7_char_overlap_count": 0,
        "user_authorization_date": "2026-08-30",
    }


def test_valid_registry_returns_a_complete_typed_persona_snapshot(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "persona.json"
    config_path.write_text(json.dumps(_valid_registry()), encoding="utf-8")

    result = load_persona(config_path)

    assert result.error_code is None
    assert result.fallback is False
    assert result.ready is True
    assert isinstance(result.snapshot, PersonaSnapshot)
    assert result.snapshot.status == "READY"
    assert result.snapshot.source == "persona_v2"
    assert result.snapshot.persona_id == "synthetic.persona"
    assert result.snapshot.profile == PersonaProfile(
        display_name="Synthetic Character",
        locale="en-US",
        summary="A complete synthetic character profile.",
        required_facets=(
            "IDENTITY",
            "BACKGROUND",
            "CORE_TRAIT",
            "AUTONOMY",
            "KNOWLEDGE_BOUNDARY",
            "EXPRESSION_STYLE",
            "RELATIONSHIP_STYLE",
            "MEMORY_CONTINUITY",
            "UNCERTAINTY",
        ),
        required_modes=("text_letter", "spoken_video", "musical_video"),
    )
    assert result.snapshot.declarations[0] == PersonaDeclaration(
        declaration_id="identity.synthetic",
        source_id="source.synthetic",
        tier="PUBLIC_CANON",
        confidence="HIGH",
        rights_status="SUMMARY_ONLY",
        allowed_public_release=True,
        statement="Synthetic contract rule.",
        mode=None,
        facet="IDENTITY",
    )


def test_public_style_exemplar_loads_as_immutable_non_factual_guidance(
    tmp_path: Path,
) -> None:
    payload = _registry_with_style_exemplar()
    payload["style_exemplar_provenance"] = _style_provenance()
    path = tmp_path / "persona.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = load_persona(path)

    assert result.ready is True
    exemplar = result.snapshot.style_exemplars[0]
    assert isinstance(exemplar, PersonaStyleExemplar)
    assert (exemplar.exemplar_id, exemplar.source_id, exemplar.situation) == (
        "style.synthetic.greeting", "source.synthetic", "brief_greeting",
    )
    assert exemplar.style_only and exemplar.user_text_is_synthetic
    assert not exemplar.factual_authority and not exemplar.assistant_text_is_verbatim


def test_style_exemplars_without_provenance_fail_schema_validation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-provenance.json"
    path.write_text(json.dumps(_registry_with_style_exemplar()), encoding="utf-8")

    result = load_persona(path)

    assert result.error_code == PersonaLoadErrorCode.SCHEMA_INVALID
    assert result.fallback is True


def test_style_exemplar_source_mismatch_fails_schema_validation(tmp_path: Path) -> None:
    payload = _registry_with_style_exemplar()
    payload["style_exemplar_provenance"] = _style_provenance("source.other")
    path = tmp_path / "source-mismatch.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = load_persona(path)

    assert result.error_code == PersonaLoadErrorCode.SCHEMA_INVALID
    assert result.fallback is True


def test_policy_only_registry_is_not_misreported_as_ready(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "p02.persona.v2",
                "persona_id": "synthetic.policy",
                "declarations": [
                    _declaration(
                        "constitution.synthetic",
                        tier="CONSTITUTION",
                        facet="POLICY",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    result = load_persona(policy_path)

    assert result.fallback is False
    assert result.policy_only is True
    assert result.snapshot.status == "POLICY_ONLY"
    assert result.error_code == PersonaLoadErrorCode.INCOMPLETE
    assert result.readiness_gaps == ("profile",)
    assert result.snapshot.declarations


def test_missing_required_facet_or_mode_reports_sanitized_gaps(tmp_path: Path) -> None:
    payload = _valid_registry()
    payload["declarations"] = [
        row
        for row in payload["declarations"]
        if row.get("facet") != "KNOWLEDGE_BOUNDARY"
        and row.get("mode") != "musical_video"
    ]
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = load_persona(path)

    assert result.snapshot.status == "POLICY_ONLY"
    assert result.error_code == PersonaLoadErrorCode.INCOMPLETE
    assert result.readiness_gaps == (
        "facet:KNOWLEDGE_BOUNDARY",
        "mode:musical_video",
    )
    assert str(tmp_path) not in repr(result)


def test_missing_registry_returns_an_empty_sanitized_draft(tmp_path: Path) -> None:
    missing_path = tmp_path / "private-user-folder" / "persona.json"

    result = load_persona(missing_path)

    assert result.error_code == PersonaLoadErrorCode.FILE_MISSING
    assert result.fallback is True
    assert result.snapshot.status == "DRAFT"
    assert result.snapshot.source == "draft"
    assert result.snapshot.persona_id is None
    assert result.snapshot.declarations == ()
    assert result.snapshot.profile is None
    assert str(missing_path) not in repr(result)


def test_invalid_registry_inputs_fail_closed_with_stable_codes(
    tmp_path: Path,
) -> None:
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
    assert (
        unavailable_schema_result.error_code
        == PersonaLoadErrorCode.SCHEMA_UNAVAILABLE
    )
    for result in (
        malformed_result,
        unreadable_result,
        schema_invalid_result,
        unavailable_schema_result,
    ):
        assert result.fallback is True
        assert result.snapshot.declarations == ()
        assert str(tmp_path) not in repr(result)


def test_release_rights_allow_summary_but_block_private_declarations(
    tmp_path: Path,
) -> None:
    schema_path = ROOT / "contracts" / "persona_v2.schema.json"

    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(_valid_registry()), encoding="utf-8")

    private_path = tmp_path / "private.json"
    private_registry = _valid_registry()
    private_registry["declarations"][0]["rights_status"] = "LOCAL_PRIVATE_ONLY"
    private_registry["declarations"][0]["allowed_public_release"] = False
    private_path.write_text(json.dumps(private_registry), encoding="utf-8")

    summary_result = load_persona(summary_path, schema_path=schema_path)
    private_result = load_persona(private_path, schema_path=schema_path)

    assert summary_result.ready is True
    assert summary_result.snapshot.declarations[0].rights_status == "SUMMARY_ONLY"
    assert private_result.fallback is True
    assert private_result.error_code == PersonaLoadErrorCode.RIGHTS_BLOCKED
    assert private_result.snapshot.declarations == ()
