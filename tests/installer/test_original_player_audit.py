from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

import tools.audit_original_players as audit_module
from tools.audit_original_players import (
    OriginalPlayerAuditError,
    audit_original_players,
)


def _write_archive(
    path: Path,
    *,
    html_name: str,
    html: str,
    javascript_name: str,
    javascript: str,
    extra: dict[str, str | bytes] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(html_name, html)
        archive.writestr(javascript_name, javascript)
        archive.writestr("assets/player.css", "video{display:block}")
        for name, content in (extra or {}).items():
            archive.writestr(name, content)


def _write_players(resources: Path, *, private_marker: str = "") -> None:
    _write_archive(
        resources / "feplayer.dat",
        html_name="index.html",
        html=(
            '<script src="assets/feplayer.js"></script>'
            '<link href="assets/player.css" rel="stylesheet">'
        ),
        javascript_name="assets/feplayer.js",
        javascript=" ".join(
            (
                'document.createElement("video")',
                "HTMLVideoElement",
                "postMessage",
                'new URLSearchParams(location.search).get("video_url")',
                '"/toy/media/letter/{id}"',
                'payload.action==="playerReady"',
                "reply_video_url videoUrl autoplay controls currentTime duration",
                "Hls hls.js .mp4 .m3u8",
                "https://media.example.invalid/player",
                private_marker,
            )
        ),
        extra={"assets/intro.mp4": b"synthetic media fixture"},
    )
    _write_archive(
        resources / "webplayer.dat",
        html_name="player.html",
        html='<script src="assets/webplayer.js"></script>',
        javascript_name="assets/webplayer.js",
        javascript=" ".join(
            (
                "<video",
                "addEventListener('message'",
                'new URLSearchParams(location.search).get("video_url")',
                'new URLSearchParams(location.search).get("letter_id")',
                "DPlayer video_url poster muted loop",
                "postMessage .webm .m3u8",
                private_marker,
            )
        ),
    )


def test_player_audit_reports_binding_metadata_without_source_leakage(tmp_path: Path) -> None:
    private_marker = "PRIVATE_PLAYER_FIXTURE_MUST_NOT_LEAK"
    resources = tmp_path / "0.0.9.615" / "resources"
    _write_players(resources, private_marker=private_marker)

    report = audit_original_players(resources)

    assert report["schema_version"] == "p03.original-player-audit.v1"
    assert report["status"] == "AUDITED"
    assert report["source"]["client_version_hint"] == "0.0.9.615"
    assert report["source"]["archive_names"] == ["feplayer.dat", "webplayer.dat"]

    feplayer = report["players"]["feplayer"]
    assert feplayer["name"] == "feplayer.dat"
    assert feplayer["archive"]["direct_media_counts"] == {".mp4": 1}
    assert feplayer["entrypoints"]["html_members"] == ["index.html"]
    assert feplayer["entrypoints"]["relative_js_css_assets"] == [
        "assets/feplayer.js",
        "assets/player.css",
    ]
    assert feplayer["entrypoints"]["javascript_bundles"][0]["member"] == "assets/feplayer.js"
    assert feplayer["binding_evidence"]["local_api_paths"] == [
        "/toy/media/letter/{id}"
    ]
    assert feplayer["binding_evidence"]["action_names"] == ["playerReady"]
    assert feplayer["binding_evidence"]["query_keys"] == ["video_url"]
    assert feplayer["binding_evidence"]["field_counts"]["reply_video_url"] == 1
    assert feplayer["binding_evidence"]["field_counts"]["videoUrl"] == 1
    assert feplayer["binding_evidence"]["transport_marker_counts"]["postMessage"] == 1
    assert feplayer["binding_evidence"]["player_marker_counts"]["Hls"] == 1
    assert feplayer["binding_evidence"]["external_origin_hosts"] == [
        "media.example.invalid"
    ]

    webplayer = report["players"]["webplayer"]
    assert webplayer["entrypoints"]["html_members"] == ["player.html"]
    assert webplayer["binding_evidence"]["query_keys"] == ["letter_id", "video_url"]
    assert webplayer["binding_evidence"]["player_marker_counts"]["DPlayer"] == 1
    assert report["comparison"]["shared_query_keys"] == ["video_url"]

    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert str(tmp_path) not in encoded
    assert private_marker not in encoded
    assert "https://media.example.invalid/player" not in encoded
    assert "media.example.invalid" in encoded
    assert "document.createElement(\"video\")" in encoded


def test_player_audit_rejects_missing_or_malformed_archives(tmp_path: Path) -> None:
    resources = tmp_path / "0.0.9.615" / "resources"
    resources.mkdir(parents=True)
    (resources / "feplayer.dat").write_bytes(b"not a zip")

    with pytest.raises(OriginalPlayerAuditError) as malformed:
        audit_original_players(resources)
    assert malformed.value.code == "PLAYER_ARCHIVE_INVALID"

    (resources / "feplayer.dat").unlink()
    with pytest.raises(OriginalPlayerAuditError) as missing:
        audit_original_players(resources)
    assert missing.value.code == "PLAYER_ARCHIVE_NOT_FOUND"


def test_player_audit_rejects_unsafe_archive_members(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    _write_players(resources)
    with zipfile.ZipFile(resources / "feplayer.dat", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", "fixture")
        archive.writestr("../outside.js", "fixture")

    with pytest.raises(OriginalPlayerAuditError) as unsafe:
        audit_original_players(resources)
    assert unsafe.value.code == "PLAYER_ARCHIVE_UNSAFE"


def test_player_audit_enforces_per_member_and_total_text_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = tmp_path / "resources"
    _write_players(resources)

    monkeypatch.setattr(audit_module, "MAX_TEXT_MEMBER_BYTES", 4)
    with pytest.raises(OriginalPlayerAuditError) as member_limit:
        audit_original_players(resources)
    assert member_limit.value.code == "PLAYER_TEXT_MEMBER_TOO_LARGE"

    monkeypatch.setattr(audit_module, "MAX_TEXT_MEMBER_BYTES", 1024 * 1024)
    monkeypatch.setattr(audit_module, "MAX_TOTAL_TEXT_BYTES", 10)
    with pytest.raises(OriginalPlayerAuditError) as total_limit:
        audit_original_players(resources)
    assert total_limit.value.code == "PLAYER_TOTAL_TEXT_TOO_LARGE"


def test_player_audit_requires_a_versioned_resources_directory(tmp_path: Path) -> None:
    with pytest.raises(OriginalPlayerAuditError) as missing:
        audit_original_players(tmp_path / "missing")
    assert missing.value.code == "PLAYER_RESOURCES_NOT_FOUND"
