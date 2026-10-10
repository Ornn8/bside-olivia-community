"""Synthetic-only regressions for the exact local native initialization adapter."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from installer import patch_local_login as module


ORIGINAL = b"synthetic-prefix-" + bytes.fromhex("0f 84 e8 01 00 00") + b"-gap-" + bytes.fromhex("74 5b") + b"-synthetic-tail"
PATCHED = ORIGINAL.replace(bytes.fromhex("74 5b"), bytes.fromhex("eb 5b"))
LEGACY = ORIGINAL.replace(bytes.fromhex("0f 84 e8 01 00 00"), bytes.fromhex("e9 e9 01 00 00 90"))


@pytest.fixture(autouse=True)
def synthetic_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_OFFSET", ORIGINAL.index(bytes.fromhex("74 5b")))
    monkeypatch.setattr(module, "_SIZE_BYTES", len(ORIGINAL))
    monkeypatch.setattr(module, "_ORIGINAL_SHA256", hashlib.sha256(ORIGINAL).hexdigest())
    monkeypatch.setattr(module, "_PATCHED_SHA256", hashlib.sha256(PATCHED).hexdigest())
    monkeypatch.setattr(module, "_LEGACY_SHA256", hashlib.sha256(LEGACY).hexdigest(), raising=False)
    monkeypatch.setattr(module, "_LEGACY_OFFSET", ORIGINAL.index(bytes.fromhex("0f 84 e8 01 00 00")), raising=False)


def installation(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "managed"
    marker = root / "local_backend/local_server.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("# synthetic managed backend\n", encoding="utf-8")
    target = root / module._RELATIVE
    target.parent.mkdir(parents=True)
    target.write_bytes(ORIGINAL)
    return root, target, target.with_name(target.name + ".local-login.orig")


def test_patch_backup_and_idempotency_preserve_personal_state(tmp_path: Path) -> None:
    root, target, backup = installation(tmp_path)
    personal = root / "data/personal.txt"
    personal.parent.mkdir()
    personal.write_bytes(b"synthetic personal state")
    assert module.apply(root) == "APPLIED"
    assert target.read_bytes() == PATCHED
    assert backup.read_bytes() == ORIGINAL
    stamp = (target.stat().st_mtime_ns, backup.stat().st_mtime_ns)
    assert module.apply(root) == "ALREADY_APPLIED"
    assert (target.stat().st_mtime_ns, backup.stat().st_mtime_ns) == stamp
    assert personal.read_bytes() == b"synthetic personal state"
    assert not tuple(target.parent.glob(".local-login-*.tmp"))


@pytest.mark.parametrize("untrusted", [b"unknown", ORIGINAL[:-1], ORIGINAL + b"x", PATCHED[:-1] + b"x"])
def test_unknown_truncated_and_modified_plugins_fail_closed(tmp_path: Path, untrusted: bytes) -> None:
    root, target, backup = installation(tmp_path)
    target.write_bytes(untrusted)
    with pytest.raises(module.LocalLoginPatchError, match="^LOCAL_LOGIN_UNSUPPORTED_PLUGIN$"):
        module.apply(root)
    assert target.read_bytes() == untrusted
    assert not backup.exists()


def test_branch_bytes_are_checked_independently_of_fingerprint(tmp_path: Path, monkeypatch) -> None:
    root, target, backup = installation(tmp_path)
    wrong = ORIGINAL.replace(bytes.fromhex("74 5b"), bytes.fromhex("75 5b"))
    monkeypatch.setattr(module, "_ORIGINAL_SHA256", hashlib.sha256(wrong).hexdigest())
    target.write_bytes(wrong)
    with pytest.raises(module.LocalLoginPatchError, match="LOCAL_LOGIN_UNSUPPORTED_PLUGIN"):
        module.apply(root)
    assert target.read_bytes() == wrong
    assert not backup.exists()


@pytest.mark.parametrize("live", [ORIGINAL, PATCHED, LEGACY])
def test_invalid_existing_backup_rejected_even_for_patched_input(tmp_path: Path, live: bytes) -> None:
    root, target, backup = installation(tmp_path)
    target.write_bytes(live)
    backup.write_bytes(b"untrusted backup")
    with pytest.raises(module.LocalLoginPatchError, match="^LOCAL_LOGIN_BACKUP_INVALID$"):
        module.apply(root)
    assert target.read_bytes() == live
    assert backup.read_bytes() == b"untrusted backup"


def test_patched_input_without_trusted_backup_fails_closed(tmp_path: Path) -> None:
    root, target, backup = installation(tmp_path)
    target.write_bytes(PATCHED)
    with pytest.raises(module.LocalLoginPatchError, match="^LOCAL_LOGIN_BACKUP_INVALID$"):
        module.apply(root)
    assert target.read_bytes() == PATCHED
    assert not backup.exists()


@pytest.mark.parametrize("with_backup", [False, True])
def test_exact_legacy_patch_migrates_from_verified_original(tmp_path: Path, with_backup: bool) -> None:
    root, target, backup = installation(tmp_path)
    target.write_bytes(LEGACY)
    if with_backup:
        backup.write_bytes(ORIGINAL)
    assert module.apply(root) == "APPLIED"
    assert target.read_bytes() == PATCHED
    assert backup.read_bytes() == ORIGINAL
    assert module.apply(root) == "ALREADY_APPLIED"
    assert module.apply(root, restore=True) == "RESTORED"
    assert target.read_bytes() == ORIGINAL


def test_legacy_fingerprint_does_not_replace_original_verification(tmp_path: Path, monkeypatch) -> None:
    root, target, backup = installation(tmp_path)
    wrong = LEGACY[:-1] + b"x"
    monkeypatch.setattr(module, "_LEGACY_SHA256", hashlib.sha256(wrong).hexdigest())
    target.write_bytes(wrong)
    with pytest.raises(module.LocalLoginPatchError, match="^LOCAL_LOGIN_UNSUPPORTED_PLUGIN$"):
        module.apply(root)
    assert target.read_bytes() == wrong
    assert not backup.exists()


def test_backup_publish_race_never_overwrites_untrusted_backup(tmp_path: Path, monkeypatch) -> None:
    root, target, backup = installation(tmp_path)
    publisher = "rename" if os.name == "nt" else "link"
    real_publish = getattr(module.os, publisher)
    def raced(source, destination):
        backup.write_bytes(b"untrusted concurrent backup")
        return real_publish(source, destination)
    monkeypatch.setattr(module.os, publisher, raced)
    with pytest.raises(module.LocalLoginPatchError, match="^LOCAL_LOGIN_BACKUP_INVALID$"):
        module.apply(root)
    assert target.read_bytes() == ORIGINAL
    assert backup.read_bytes() == b"untrusted concurrent backup"
    assert not tuple(target.parent.glob(".local-login-*.tmp"))


@pytest.mark.parametrize("destination", ["backup", "target"])
def test_locked_atomic_publish_keeps_original_and_no_partial_backup(tmp_path: Path, monkeypatch, destination: str) -> None:
    root, target, backup = installation(tmp_path)
    publisher = "rename" if os.name == "nt" else "link"
    real_replace, real_publish = module.os.replace, getattr(module.os, publisher)
    def locked(source, dest):
        if Path(dest) == (backup if destination == "backup" else target):
            raise PermissionError("private native file path")
        return real_replace(source, dest)
    monkeypatch.setattr(module.os, "replace", locked)
    def locked_publish(source, dest):
        if destination == "backup":
            raise PermissionError("private native file path")
        return real_publish(source, dest)
    monkeypatch.setattr(module.os, publisher, locked_publish)
    with pytest.raises(module.LocalLoginPatchError, match="^LOCAL_LOGIN_FILE_UNAVAILABLE_CLOSE_CLIENT_AND_RETRY$"):
        module.apply(root)
    assert target.read_bytes() == ORIGINAL
    if destination == "target":
        assert backup.read_bytes() == ORIGINAL
    else:
        assert not backup.exists()
    assert not tuple(target.parent.glob(".local-login-*.tmp"))


def test_unmanaged_source_is_rejected_without_mutation(tmp_path: Path) -> None:
    root, target, backup = installation(tmp_path)
    (root / "local_backend/local_server.py").unlink()
    with pytest.raises(module.LocalLoginPatchError, match="^LOCAL_LOGIN_MANAGED_INSTALL_REQUIRED$"):
        module.apply(root)
    assert target.read_bytes() == ORIGINAL
    assert not backup.exists()


def test_restore_requires_exact_backup_and_is_idempotent(tmp_path: Path) -> None:
    root, target, backup = installation(tmp_path)
    module.apply(root)
    assert module.apply(root, restore=True) == "RESTORED"
    assert target.read_bytes() == backup.read_bytes() == ORIGINAL
    assert module.apply(root, restore=True) == "ALREADY_ORIGINAL"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction containment")
def test_reparse_parent_is_rejected(tmp_path: Path) -> None:
    import subprocess
    root, target, backup = installation(tmp_path)
    outside = tmp_path / "outside"
    target.parent.rename(outside)
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(target.parent), str(outside)], capture_output=True)
    if result.returncode:
        pytest.skip("junction creation unavailable")
    with pytest.raises(module.LocalLoginPatchError, match="^LOCAL_LOGIN_UNSAFE_PATH$"):
        module.apply(root)
    assert (outside / target.name).read_bytes() == ORIGINAL
    assert not (outside / backup.name).exists()
