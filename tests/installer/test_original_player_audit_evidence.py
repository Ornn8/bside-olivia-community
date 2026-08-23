from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "evidence" / "original-player-audit.0.0.9.615.json"


def _load() -> dict[str, object]:
    value = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_original_player_evidence_is_for_supported_client_and_both_archives() -> None:
    report = _load()

    assert report["schema_version"] == "p03.original-player-audit.v1"
    assert report["status"] == "AUDITED"
    assert report["source"] == {
        "archive_names": ["feplayer.dat", "webplayer.dat"],
        "client_version_hint": "0.0.9.615",
    }

    players = report["players"]
    assert set(players) == {"feplayer", "webplayer"}
    assert players["feplayer"]["archive_sha256"] == (
        "0289b883a62c391f0f1d24b6088a7fb07d592a98c5b0f6eff3ab931561be0848"
    )
    assert players["webplayer"]["archive_sha256"] == (
        "504b59876af2f04c4902f8c8e6811018d36a2da4394e20cf74f22d13d394b636"
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
    text = EVIDENCE.read_text(encoding="utf-8")
    lowered = text.casefold()

    assert not re.search(r"[a-z]:[/\\]", text, flags=re.IGNORECASE)
    assert "document.createelement" not in lowered
    assert "<script" not in lowered
    assert "function(" not in lowered
    assert "=>" not in text
    assert "api_key" not in lowered
    assert "bearer " not in lowered
    assert "sk-" not in lowered

    # Only sanitized host names may remain; no complete external URL is stored.
    assert "https://" not in lowered
    assert "http://" not in lowered
    assert "localhost" in lowered
