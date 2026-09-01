from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from patch_feapp import (
    HE_ANCHOR,
    INJECT_ANCHOR,
    MAILBOX_LOGIN_ANCHOR,
    MAILBOX_LOGIN_REPLACEMENT,
    MAIN_JS,
    HE_ANCHOR_0627,
    INJECT_ANCHOR_0627,
    MAILBOX_LOGIN_ANCHOR_0627,
    MAIN_JS_0627,
    WEB_PLAYER_PLAYLIST_EVENT_ANCHOR_0627,
    WEB_PLAYER_PLAYLIST_EVENT_BROKEN_INTEGER_0627,
    WEB_PLAYER_LOCAL_CHECK_ANCHOR_0627,
    WEB_PLAYER_LOCAL_CHECK_BROKEN_INTEGER_0627,
    WEB_PLAYER_LOCAL_CHECK_REPLACEMENT_0627,
    WEB_PLAYER_SONGLIST_EVENT_ANCHOR_0627,
    WEB_PLAYER_SONGLIST_EVENT_BROKEN_INTEGER_0627,
    _PATCH_PROFILES,
    _patch_web_player_event_ids,
    repair_web_player_event_ids,
)
from tools.audit_original_client import (
    OriginalClientAuditError,
    audit_original_client,
)


def _write_archive(path: Path, javascript: str, *, extra: dict[str, str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MAIN_JS, javascript)
        archive.writestr("assets/app.css", "body{display:block}")
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_audit_reports_sanitized_original_client_evidence(tmp_path: Path) -> None:
    private_marker = "PRIVATE_FIXTURE_TEXT_SHOULD_NOT_LEAK"
    javascript = " ".join(
        (
            HE_ANCHOR,
            INJECT_ANCHOR,
            MAILBOX_LOGIN_ANCHOR,
            'router.push({name:ye.Settings})',
            'router.replace({name:"LetterDetail"})',
            'e.action==="getClientConfig"',
            '"/toy/letter/list"',
            '"/toy/letter/detail"',
            '"/toy/letter/send"',
            "letter_status reply_content reply_video_url media_status",
            private_marker,
        )
    )
    archive = _write_archive(
        tmp_path / "0.0.9.615" / "resources" / "feapp.dat",
        javascript,
    )

    report = audit_original_client(archive, expected_sha256=_sha256(archive))

    assert report["status"] == "AUDITED"
    assert report["source"]["name"] == "feapp.dat"
    assert report["source"]["client_version_hint"] == "0.0.9.615"
    assert report["source"]["expected_sha256_match"] is True
    assert report["patch_contract"]["state"] == "official_patch_ready"
    assert report["patch_contract"]["safe_to_apply_existing_patch"] is True
    assert report["navigation_evidence"]["has_home"] is True
    assert report["navigation_evidence"]["has_collection"] is False
    assert "ye.Home" in report["navigation_evidence"]["route_references"]
    assert "ye.Settings" in report["navigation_evidence"]["route_references"]
    assert "LetterDetail" in report["navigation_evidence"]["route_references"]
    assert report["local_contract_evidence"]["toy_api_paths"] == [
        "/toy/letter/detail",
        "/toy/letter/list",
        "/toy/letter/send",
    ]
    assert report["local_contract_evidence"]["action_names"] == [
        "getClientConfig"
    ]
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert str(tmp_path) not in encoded
    assert private_marker not in encoded
    assert HE_ANCHOR not in encoded
    assert MAILBOX_LOGIN_ANCHOR not in encoded


def test_audit_supports_original_client_0_0_9_627(tmp_path: Path) -> None:
    archive = tmp_path / "0.0.9.627" / "resources" / "feapp.dat"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(
            MAIN_JS_0627,
            HE_ANCHOR_0627 + INJECT_ANCHOR_0627 + MAILBOX_LOGIN_ANCHOR_0627,
        )

    report = audit_original_client(archive)

    assert report["source"]["client_version_hint"] == "0.0.9.627"
    assert report["main_bundle"]["member"] == MAIN_JS_0627
    assert report["patch_contract"]["safe_to_apply_existing_patch"] is True


def test_0627_web_player_event_ids_are_strings_and_idempotent() -> None:
    profile = next(item for item in _PATCH_PROFILES if item.main_js == MAIN_JS_0627)
    original = (
        WEB_PLAYER_PLAYLIST_EVENT_ANCHOR_0627
        + " fixture "
        + WEB_PLAYER_SONGLIST_EVENT_ANCHOR_0627
        + WEB_PLAYER_LOCAL_CHECK_ANCHOR_0627
    )

    patched = _patch_web_player_event_ids(original, profile)

    assert WEB_PLAYER_PLAYLIST_EVENT_ANCHOR_0627 in patched
    assert WEB_PLAYER_SONGLIST_EVENT_ANCHOR_0627 in patched
    assert WEB_PLAYER_LOCAL_CHECK_REPLACEMENT_0627 in patched
    assert _patch_web_player_event_ids(patched, profile) == patched

    broken = patched.replace(
        WEB_PLAYER_PLAYLIST_EVENT_ANCHOR_0627,
        WEB_PLAYER_PLAYLIST_EVENT_BROKEN_INTEGER_0627,
    ).replace(
        WEB_PLAYER_SONGLIST_EVENT_ANCHOR_0627,
        WEB_PLAYER_SONGLIST_EVENT_BROKEN_INTEGER_0627,
    ).replace(
        WEB_PLAYER_LOCAL_CHECK_REPLACEMENT_0627,
        WEB_PLAYER_LOCAL_CHECK_BROKEN_INTEGER_0627,
    )
    assert _patch_web_player_event_ids(broken, profile) == patched


def test_0627_web_player_event_archive_repair_is_idempotent(tmp_path: Path) -> None:
    archive_path = tmp_path / "feapp.dat"
    javascript = (
        HE_ANCHOR_0627
        + INJECT_ANCHOR_0627
        + MAILBOX_LOGIN_ANCHOR_0627
        + WEB_PLAYER_PLAYLIST_EVENT_ANCHOR_0627
        + WEB_PLAYER_SONGLIST_EVENT_ANCHOR_0627
        + WEB_PLAYER_LOCAL_CHECK_ANCHOR_0627
    )
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MAIN_JS_0627, javascript)

    assert repair_web_player_event_ids(archive_path, work_root=tmp_path) == "PATCHED"
    assert (
        repair_web_player_event_ids(archive_path, work_root=tmp_path)
        == "ALREADY_PATCHED"
    )


def test_audit_identifies_an_already_patched_archive_without_repatching(tmp_path: Path) -> None:
    javascript = " ".join(
        (
            HE_ANCHOR,
            INJECT_ANCHOR,
            MAILBOX_LOGIN_REPLACEMENT,
            "toyApiUrl toyWsUrl",
        )
    )
    archive = _write_archive(tmp_path / "feapp.dat", javascript)

    report = audit_original_client(archive)

    assert report["patch_contract"]["state"] == "already_patched"
    assert report["patch_contract"]["safe_to_apply_existing_patch"] is False
    assert report["navigation_evidence"]["has_collection"] is False


def test_audit_rejects_missing_main_bundle_and_unsafe_members(tmp_path: Path) -> None:
    missing = tmp_path / "missing-main.dat"
    with zipfile.ZipFile(missing, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("assets/other.js", "fixture")
    with pytest.raises(OriginalClientAuditError) as missing_error:
        audit_original_client(missing)
    assert missing_error.value.code == "CLIENT_MAIN_BUNDLE_MISSING"

    unsafe = tmp_path / "unsafe.dat"
    with zipfile.ZipFile(unsafe, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MAIN_JS, HE_ANCHOR + INJECT_ANCHOR + MAILBOX_LOGIN_ANCHOR)
        archive.writestr("../outside.js", "fixture")
    with pytest.raises(OriginalClientAuditError) as unsafe_error:
        audit_original_client(unsafe)
    assert unsafe_error.value.code == "CLIENT_ARCHIVE_UNSAFE"


def test_audit_rejects_invalid_expected_hash_without_reading_source(tmp_path: Path) -> None:
    archive = _write_archive(
        tmp_path / "feapp.dat",
        HE_ANCHOR + INJECT_ANCHOR + MAILBOX_LOGIN_ANCHOR,
    )
    with pytest.raises(OriginalClientAuditError) as error:
        audit_original_client(archive, expected_sha256="not-a-sha")
    assert error.value.code == "CLIENT_EXPECTED_HASH_INVALID"
