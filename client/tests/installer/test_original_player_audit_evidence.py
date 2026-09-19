from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = (
    ROOT
    / "docs"
    / "evidence"
    / "original-player-audit.0.0.9.627.json"
)
MANIFEST = ROOT / "installer" / "full-patch-manifest.json"


def _load() -> dict[str, object]:
    value = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_string_values(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_string_values(child))
        return result
    return []


def test_original_player_evidence_is_for_supported_client_and_both_archives() -> None:
    report = _load()

    assert report["schema_version"] == "p03.original-player-audit.v1"
    assert report["status"] == "AUDITED"
    assert report["source"] == {
        "archive_names": ["feplayer.dat", "webplayer.dat"],
        "client_version_hint": "0.0.9.627",
    }

    players = report["players"]
    assert set(players) == {"feplayer", "webplayer"}
    assert players["feplayer"]["archive_sha256"] == (
        "0289b883a62c391f0f1d24b6088a7fb07d592a98c5b0f6eff3ab931561be0848"
    )
    assert players["webplayer"]["archive_sha256"] == (
        "565b5e3e113c2a9dfb90d5fa4f2a0ccda9b0151c118ae3365e6ee0c8624a451d"
    )


def test_installer_pins_the_audited_supported_webplayer() -> None:
    report = _load()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["client_version"] == report["source"]["client_version_hint"]
    assert manifest["webplayer_sha256"] == (
        report["players"]["webplayer"]["archive_sha256"]
    )


def test_original_players_are_distinct_and_share_no_proven_binding_contract() -> None:
    report = _load()
    feplayer = report["players"]["feplayer"]
    webplayer = report["players"]["webplayer"]

    assert feplayer["archive"] == {
        "compressed_bytes": 1362,
        "direct_media_counts": {},
        "extension_counts": {".html": 1},
        "member_count": 1,
        "uncompressed_bytes": 5012,
    }
    assert feplayer["entrypoints"] == {
        "html_members": ["index.html"],
        "javascript_bundles": [],
        "relative_js_css_assets": [],
    }

    assert webplayer["archive"]["extension_counts"] == {
        ".css": 2,
        ".html": 1,
        ".js": 4,
    }
    assert webplayer["archive"]["member_count"] == 7
    assert len(webplayer["entrypoints"]["javascript_bundles"]) == 4

    assert report["comparison"] == {
        "shared_html_members": ["index.html"],
        "shared_local_api_paths": [],
        "shared_query_keys": [],
    }


def test_evidence_proves_query_transport_markers_but_not_query_keys_or_reply_fields() -> None:
    report = _load()

    feplayer = report["players"]["feplayer"]["binding_evidence"]
    assert feplayer["player_marker_counts"]["<video"] == 1
    assert feplayer["transport_marker_counts"]["URLSearchParams"] == 1
    assert feplayer["transport_marker_counts"]["location.search"] == 1
    assert feplayer["query_keys"] == []
    assert feplayer["local_api_paths"] == []

    webplayer = report["players"]["webplayer"]["binding_evidence"]
    assert webplayer["transport_marker_counts"]["URLSearchParams"] == 2
    assert webplayer["transport_marker_counts"]["location.search"] == 3
    assert webplayer["query_keys"] == []
    assert webplayer["local_api_paths"] == []

    for player in (feplayer, webplayer):
        assert player["field_counts"]["reply_video_url"] == 0
        assert player["field_counts"]["replyVideoUrl"] == 0
        assert player["field_counts"]["video_url"] == 0
        assert player["field_counts"]["videoUrl"] == 0
        assert player["transport_marker_counts"]["postMessage"] == 0
        assert player["transport_marker_counts"]["localStorage"] == 0
        assert player["transport_marker_counts"]["sessionStorage"] == 0
        assert player["transport_marker_counts"]["window.name"] == 0


def test_evidence_is_sanitized_and_contains_no_original_source_dump() -> None:
    report = _load()
    values = "\n".join(_string_values(report))
    lowered = values.casefold()

    # Marker names are approved schema keys. Source fragments must never appear
    # as report values.
    assert not re.search(r"[a-z]:[/\\]", values, flags=re.IGNORECASE)
    assert "document.createelement" not in lowered
    assert "<script" not in lowered
    assert "function(" not in lowered
    assert "=>" not in values
    assert "api_key" not in lowered
    assert "bearer " not in lowered
    assert "sk-" not in lowered

    # Only sanitized host names may remain; no complete external URL is stored.
    assert "https://" not in lowered
    assert "http://" not in lowered
    assert "localhost" in lowered
