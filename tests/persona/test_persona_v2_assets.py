from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError


ROOT = Path(__file__).resolve().parents[2]


def test_v2_constitution_uses_the_merged_p02_01_schema() -> None:
    schema = json.loads((ROOT / "contracts" / "persona_v2.schema.json").read_text(encoding="utf-8"))
    constitution_path = ROOT / "linli_character" / "persona_v2.json"
    constitution = json.loads(constitution_path.read_text(encoding="utf-8"))

    assert constitution["schema_version"] == "p02.persona.v2"
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(constitution)


def test_v2_provenance_has_a_closed_schema_contract() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "persona_v2_provenance.schema.json").read_text(encoding="utf-8")
    )
    provenance = json.loads(
        (ROOT / "linli_character" / "provenance_v2.json").read_text(encoding="utf-8")
    )

    assert provenance["schema_version"] == "p02.persona.v2"
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(provenance)


def test_v2_declarations_and_provenance_form_a_bidirectional_registry() -> None:
    constitution = json.loads(
        (ROOT / "linli_character" / "persona_v2.json").read_text(encoding="utf-8")
    )
    provenance = json.loads(
        (ROOT / "linli_character" / "provenance_v2.json").read_text(encoding="utf-8")
    )
    declarations = constitution["declarations"]
    sources = provenance["sources"]
    declaration_ids = {item["declaration_id"] for item in declarations}
    source_ids = {item["source_id"] for item in sources}
    linked_declaration_ids = {
        declaration_id
        for source in sources
        for declaration_id in source["declaration_ids"]
    }

    assert len(declaration_ids) == len(declarations)
    assert len(source_ids) == len(sources)
    assert {item["source_id"] for item in declarations} <= source_ids
    assert linked_declaration_ids == declaration_ids
    assert {
        "constitution.source_hierarchy",
        "fact.public_name.s002",
        "fact.public_name.s004",
        "fact.letter_music_context",
        "fact.public_music_context",
        "inference.music_time_imagery",
        "inference.concise_gentle_invitation",
    } <= declaration_ids


def test_v2_provenance_migrates_the_sanitized_legacy_source_set() -> None:
    provenance = json.loads(
        (ROOT / "linli_character" / "provenance_v2.json").read_text(encoding="utf-8")
    )
    sources = {item["source_id"]: item for item in provenance["sources"]}
    evidence = {item["evidence_id"]: item for item in provenance["evidence"]}

    assert set(sources) == {"P02.CONSTITUTION", "S001", "S002", "S003", "S004"}
    assert set(evidence) == {"S002.summary", "S004.summary"}
    assert not sources["S001"]["declaration_ids"]
    assert not sources["S003"]["declaration_ids"]
    assert {item["source_id"] for item in evidence.values()} <= set(sources)
    assert all(source["rights_status"] != "REDISTRIBUTABLE" for source_id, source in sources.items() if source_id != "P02.CONSTITUTION")


def test_v2_constitution_contains_only_short_abstract_audit_rules() -> None:
    constitution = json.loads(
        (ROOT / "linli_character" / "persona_v2.json").read_text(encoding="utf-8")
    )
    declarations = {
        item["declaration_id"]: item
        for item in constitution["declarations"]
        if item["tier"] == "CONSTITUTION"
    }

    assert {
        "constitution.source_hierarchy",
        "constitution.no_invention",
        "constitution.unknown_is_not_event",
        "constitution.public_canon_rights",
        "constitution.control_character_views",
        "constitution.local_continuation_awareness",
        "constitution.nickname_permission",
        "constitution.access_boundary",
        "constitution.low_bandwidth",
        "constitution.no_private_claims",
        "constitution.no_hidden_fields",
        "constitution.no_long_source_copy",
        "constitution.respectful_relationship",
        "constitution.no_professional_claims",
    } <= set(declarations)
    assert all(len(item["statement"]) <= 240 for item in declarations.values())


def test_v2_release_boundary_blocks_external_claims_and_keeps_private_state_empty() -> None:
    constitution = json.loads(
        (ROOT / "linli_character" / "persona_v2.json").read_text(encoding="utf-8")
    )
    provenance = json.loads(
        (ROOT / "linli_character" / "provenance_v2.json").read_text(encoding="utf-8")
    )
    source_by_id = {item["source_id"]: item for item in provenance["sources"]}
    declarations = constitution["declarations"]

    for declaration in declarations:
        source = source_by_id[declaration["source_id"]]
        if declaration["source_id"] != "P02.CONSTITUTION":
            assert source["rights_status"] == "UNKNOWN_BLOCK_RELEASE"
            assert declaration["rights_status"] == "UNKNOWN_BLOCK_RELEASE"
            assert declaration["allowed_public_release"] is False

    assert all(
        declaration["allowed_public_release"] is True
        for declaration in declarations
        if declaration["source_id"] == "P02.CONSTITUTION"
    )
    assert not any(
        declaration["tier"] in {"LOCAL_PRIVATE_ONLY", "LOCAL_CONTINUATION"}
        for declaration in declarations
    )
    assert not any(
        source["declaration_ids"]
        for source_id, source in source_by_id.items()
        if source_id in {"S001", "S003"}
    )


def test_v2_redistributable_constitution_separates_content_and_rights_provenance() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "persona_v2_provenance.schema.json").read_text(encoding="utf-8")
    )
    provenance_path = ROOT / "linli_character" / "provenance_v2.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    constitution_path = ROOT / "linli_character" / "persona_v2.json"
    constitution_source = next(
        source for source in provenance["sources"] if source["source_id"] == "P02.CONSTITUTION"
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert constitution_source["source_url"].endswith(
        "/blob/a91dc0309d2bbe0a004ba4a84f55fa93713b15ce/linli_character/persona_v2.json"
    )
    assert constitution_source["content_source"] == {
        "repository": "Ornn8/bside-olivia-local",
        "path": "linli_character/persona_v2.json",
        "revision": "a91dc0309d2bbe0a004ba4a84f55fa93713b15ce",
        "sha256": hashlib.sha256(constitution_path.read_bytes()).hexdigest(),
    }
    assert constitution_source["rights_basis"] == {
        "basis_type": "repository_license",
        "source_url": "https://github.com/Ornn8/bside-olivia-local/blob/27d001ccd6ed17e8a39c776e03d8946631858133/LICENSE",
        "revision": "27d001ccd6ed17e8a39c776e03d8946631858133",
        "status": "CONFIRMED",
    }

    for field in ("content_source", "rights_basis"):
        invalid = deepcopy(provenance)
        invalid_source = next(
            source for source in invalid["sources"] if source["source_id"] == "P02.CONSTITUTION"
        )
        invalid_source.pop(field)
        with pytest.raises(ValidationError):
            validator.validate(invalid)
