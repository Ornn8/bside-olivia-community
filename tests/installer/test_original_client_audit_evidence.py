from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "evidence" / "original-client-audit-0.0.9.615.json"
MANIFEST = ROOT / "installer" / "full-patch-manifest.json"


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_original_client_audit_matches_supported_manifest() -> None:
    evidence = _load(EVIDENCE)
    manifest = _load(MANIFEST)
    source = evidence["source"]
    assert isinstance(source, dict)

    assert evidence["schema_version"] == "p03.original-client-audit.v1"
    assert evidence["status"] == "AUDITED"
    assert source["name"] == "feapp.dat"
    assert source["client_version_hint"] == manifest["client_version"]
    assert source["archive_sha256"] == manifest["feapp_sha256"]
    assert source["expected_sha256_match"] is True


def test_original_client_patch_anchors_are_unique_and_unmodified() -> None:
    evidence = _load(EVIDENCE)
    patch = evidence["patch_contract"]
    assert isinstance(patch, dict)
    anchors = patch["anchor_counts"]
    assert isinstance(anchors, dict)

    assert patch["state"] == "official_patch_ready"
    assert patch["safe_to_apply_existing_patch"] is True
    assert anchors == {
        "local_endpoint_injection": 1,
        "mailbox_original": 1,
        "mailbox_patched": 0,
        "response_dispatch": 1,
    }


def test_original_client_evidence_supports_collection_and_user_info_only() -> None:
    evidence = _load(EVIDENCE)
    navigation = evidence["navigation_evidence"]
    assert isinstance(navigation, dict)
    routes = navigation["route_references"]
    assert isinstance(routes, list)

    assert navigation["has_home"] is True
    assert navigation["has_collection"] is True
    assert "ye.Home" in routes
    assert "ye.Collection" in routes
    assert "ye.UserInfo" in routes
    assert "LetterDetail" not in routes
    assert "ye.Settings" not in routes


def test_original_client_wire_evidence_does_not_claim_media_support() -> None:
    evidence = _load(EVIDENCE)
    contract = evidence["local_contract_evidence"]
    assert isinstance(contract, dict)
    counts = contract["wire_field_counts"]
    assert isinstance(counts, dict)

    assert counts["letter_status"] > 0
    assert counts["reply_content"] > 0
    assert counts["reply_video_url"] == 0
    assert counts["media_status"] == 0
    assert counts["media_error_code"] == 0
    assert contract["action_names"] == []
    assert contract["toy_api_paths"] == []


def test_original_client_evidence_is_sanitized() -> None:
    raw = EVIDENCE.read_text(encoding="utf-8")
    lowered = raw.casefold()

    assert "c:\\" not in lowered
    assert "d:\\" not in lowered
    assert "f:\\" not in lowered
    assert "sk-" not in lowered
    assert "api_key" not in lowered
    assert "PRIVATE_FIXTURE_TEXT_SHOULD_NOT_LEAK" not in raw
