from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from patch_feapp import MAIN_JS
from tools.audit_original_player_bridge import (
    OriginalPlayerBridgeAuditError,
    audit_original_player_bridge,
)


def _write_archive(path: Path, members: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _make_resources(root: Path, *, private_marker: str = "") -> Path:
    resources = root / "0.0.9.615" / "resources"
    _write_archive(
        resources / "feapp.dat",
        {
            MAIN_JS: " ".join(
                (
                    "ye.Collection letter_status reply_content",
                    'const params=new URLSearchParams(location.search);',
                    'const letter=params.get("letter_id");',
                    'const target="feplayer.dat?media_id="+encodeURIComponent(letter);',
                    "window.open(target);",
                    'const fallback="webplayer.dat";',
                    'const operation="openVideoPlayer";',
                    private_marker,
                )
            )
        },
    )
    _write_archive(
        resources / "feplayer.dat",
        {
            "index.html": " ".join(
                (
                    "<video autoplay muted></video>",
                    'const query=new URLSearchParams(location.search);',
                    'const media=query.get("media_id");',
                    private_marker,
                )
            )
        },
    )
    _write_archive(
        resources / "webplayer.dat",
        {
            "index.html": '<script src="assets/main.js"></script>',
            "assets/main.js": " ".join(
                (
                    'const search=new URL(location.href).searchParams;',
                    'const source=search.get("source_id");',
                    'const component="videoPlayer";',
                    private_marker,
                )
            ),
            "assets/vendor-vue.js": "vendor source should be ignored",
        },
    )
    return resources


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _all_string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_all_string_values(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_all_string_values(child))
        return result
    return []


def test_bridge_audit_correlates_main_and_player_query_contracts(tmp_path: Path) -> None:
    private_marker = "PRIVATE_FIXTURE_SHOULD_NOT_LEAK"
    resources = _make_resources(tmp_path, private_marker=private_marker)
    before = {
        path.name: _sha256(path)
        for path in resources.glob("*.dat")
    }

    report = audit_original_player_bridge(resources)

    assert report["schema_version"] == "p03.original-player-bridge-audit.v1"
    assert report["status"] == "AUDITED"
    assert report["source"] == {
        "client_version_hint": "0.0.9.615",
        "archive_names": ["feapp.dat", "feplayer.dat", "webplayer.dat"],
    }
    assert report["bridge_state"] == "literal_reference_with_query_contract_candidate"

    main = report["main_bundle"]
    assert main["query_keys"] == ["letter_id", "media_id"]
    assert main["player_reference_counts"]["feplayer.dat"] == 1
    assert main["player_reference_counts"]["webplayer.dat"] == 1
    assert main["launch_marker_counts"]["window.open"] == 1
    assert main["launch_marker_counts"]["encodeURIComponent"] == 1
    assert "feplayer.dat?media_id=" in main["technical_literals"]
    assert "openVideoPlayer" in main["technical_literals"]
    assert main["player_reference_contexts"]["feplayer.dat"]["contexts"]

    assert report["players"]["feplayer"]["query_keys"] == ["media_id"]
    assert report["players"]["webplayer"]["query_keys"] == ["source_id"]
    assert report["correlation"]["feplayer"]["shared_query_keys"] == ["media_id"]
    assert report["correlation"]["webplayer"]["shared_query_keys"] == []

    after = {
        path.name: _sha256(path)
        for path in resources.glob("*.dat")
    }
    assert after == before

    values = "\n".join(_all_string_values(report))
    assert str(tmp_path) not in values
    assert private_marker not in values
    assert "const params=" not in values
    assert "window.open(target)" not in values


def test_bridge_audit_detects_generic_query_parsing_without_inventing_keys(
    tmp_path: Path,
) -> None:
    resources = _make_resources(tmp_path)
    _write_archive(
        resources / "webplayer.dat",
        {
            "index.html": '<script src="assets/main.js"></script>',
            "assets/main.js": (
                "const all=Object.fromEntries(new URLSearchParams(location.search));"
            ),
        },
    )

    report = audit_original_player_bridge(resources)
    webplayer = report["players"]["webplayer"]

    assert webplayer["query_keys"] == []
    assert webplayer["generic_query_marker_counts"]["Object.fromEntries"] == 1
    assert webplayer["generic_query_marker_counts"]["URLSearchParams"] == 1
    assert webplayer["generic_query_marker_counts"]["location.search"] == 1


def test_bridge_audit_rejects_missing_malformed_and_unsafe_archives(
    tmp_path: Path,
) -> None:
    resources = tmp_path / "0.0.9.615" / "resources"
    resources.mkdir(parents=True)

    with pytest.raises(OriginalPlayerBridgeAuditError) as missing:
        audit_original_player_bridge(resources)
    assert missing.value.code == "BRIDGE_ARCHIVE_NOT_FOUND"

    (resources / "feapp.dat").write_bytes(b"not a zip")
    with pytest.raises(OriginalPlayerBridgeAuditError) as malformed:
        audit_original_player_bridge(resources)
    assert malformed.value.code == "BRIDGE_ARCHIVE_INVALID"

    _write_archive(resources / "feapp.dat", {MAIN_JS: "fixture"})
    _write_archive(resources / "feplayer.dat", {"../outside.js": "fixture"})
    _write_archive(resources / "webplayer.dat", {"index.html": "fixture"})
    with pytest.raises(OriginalPlayerBridgeAuditError) as unsafe:
        audit_original_player_bridge(resources)
    assert unsafe.value.code == "BRIDGE_ARCHIVE_UNSAFE"


def test_bridge_audit_report_contains_no_full_urls_or_source_dump(tmp_path: Path) -> None:
    resources = _make_resources(tmp_path)
    _write_archive(
        resources / "webplayer.dat",
        {
            "index.html": '<script src="assets/main.js"></script>',
            "assets/main.js": (
                'const endpoint="https://private.example.invalid/video";'
                'const player="videoPlayer";'
            ),
        },
    )

    report = audit_original_player_bridge(resources)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)

    assert "https://" not in encoded
    assert "private.example.invalid" not in encoded
    assert 'const endpoint=' not in encoded
    assert '"videoPlayer"' in encoded
