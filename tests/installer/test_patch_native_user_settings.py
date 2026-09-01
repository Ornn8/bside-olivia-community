from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from installer.patch_native_user_settings import (
    FALSE_WIDGET_PAIR,
    TRUE_WIDGET_PAIR,
    NativeUserSettingsPatchError,
    patch_native_user_settings,
)


def _settings_blob(
    widget_pair: bytes,
    *,
    prefix: bytes = b"\xb8\xb3\x80\x01\x0dcurrent-format",
    suffix: bytes = b"\x12synthetic-tail",
    physical_size: int = 4096,
) -> bytes:
    payload = prefix + widget_pair + suffix
    logical = struct.pack("<I", len(payload)) + payload
    assert len(logical) <= physical_size
    return logical + (b"\x00" * (physical_size - len(logical)))


def _error_code(settings: Path) -> str:
    with pytest.raises(NativeUserSettingsPatchError) as raised:
        patch_native_user_settings(settings)
    assert str(raised.value) == raised.value.code
    assert str(settings) not in str(raised.value)
    return raised.value.code


def test_unique_disabled_widgets_are_enabled_atomically_and_backed_up(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "usersettings.dat"
    original = _settings_blob(FALSE_WIDGET_PAIR)
    settings.write_bytes(original)

    result = patch_native_user_settings(settings)

    patched = settings.read_bytes()
    assert result.status == "patched"
    assert set(vars(result)) == {"status", "original_sha256", "patched_sha256"}
    assert len(patched) == len(original) == 4096
    assert struct.unpack_from("<I", patched)[0] == (
        struct.unpack_from("<I", original)[0] - 2
    )
    assert patched.count(FALSE_WIDGET_PAIR) == 0
    assert patched.count(TRUE_WIDGET_PAIR) == 1
    logical_end = 4 + struct.unpack_from("<I", patched)[0]
    assert not any(patched[logical_end:])
    assert settings.with_name("usersettings.dat.native-nav.orig").read_bytes() == original


def test_format_prefix_is_not_hard_coded(tmp_path: Path) -> None:
    settings = tmp_path / "usersettings.dat"
    settings.write_bytes(
        _settings_blob(FALSE_WIDGET_PAIR, prefix=b"\x00\xff\x10different-version")
    )

    assert patch_native_user_settings(settings).status == "patched"
    assert settings.read_bytes().count(TRUE_WIDGET_PAIR) == 1


def test_missing_settings_are_skipped_without_creating_sidecars(tmp_path: Path) -> None:
    settings = tmp_path / "usersettings.dat"

    result = patch_native_user_settings(settings)

    assert result.status == "missing"
    assert set(vars(result)) == {"status", "original_sha256", "patched_sha256"}
    assert list(tmp_path.iterdir()) == []


def test_already_enabled_settings_are_left_byte_exact(tmp_path: Path) -> None:
    settings = tmp_path / "usersettings.dat"
    original = _settings_blob(TRUE_WIDGET_PAIR)
    settings.write_bytes(original)

    assert patch_native_user_settings(settings).status == "already_enabled"
    assert settings.read_bytes() == original
    assert not settings.with_name("usersettings.dat.native-nav.orig").exists()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (FALSE_WIDGET_PAIR + FALSE_WIDGET_PAIR, "USER_SETTINGS_WIDGET_PAIR_AMBIGUOUS"),
        (b"no-widget-fields", "USER_SETTINGS_WIDGET_PAIR_UNSUPPORTED"),
    ],
)
def test_unknown_or_ambiguous_widget_layout_fails_without_mutation(
    tmp_path: Path,
    payload: bytes,
    expected: str,
) -> None:
    settings = tmp_path / "usersettings.dat"
    original = _settings_blob(payload)
    settings.write_bytes(original)

    assert _error_code(settings) == expected
    assert settings.read_bytes() == original
    assert not settings.with_name("usersettings.dat.native-nav.orig").exists()


@pytest.mark.parametrize(
    "original",
    [
        b"bad",
        struct.pack("<I", 9999) + b"short",
        struct.pack("<I", 1) + b"x" + b"\x01padding",
    ],
)
def test_invalid_file_structure_fails_closed(tmp_path: Path, original: bytes) -> None:
    settings = tmp_path / "usersettings.dat"
    settings.write_bytes(original)

    assert _error_code(settings) in {
        "USER_SETTINGS_HEADER_INVALID",
        "USER_SETTINGS_PADDING_INVALID",
    }
    assert settings.read_bytes() == original


def test_hardlinked_target_is_rejected_without_mutation(tmp_path: Path) -> None:
    settings = tmp_path / "usersettings.dat"
    original = _settings_blob(FALSE_WIDGET_PAIR)
    settings.write_bytes(original)
    os.link(settings, tmp_path / "second-name.dat")

    assert _error_code(settings) == "USER_SETTINGS_UNSAFE_PATH"
    assert settings.read_bytes() == original


def test_symlinked_target_is_rejected_without_mutation(tmp_path: Path) -> None:
    real = tmp_path / "real.dat"
    real.write_bytes(_settings_blob(FALSE_WIDGET_PAIR))
    settings = tmp_path / "usersettings.dat"
    try:
        settings.symlink_to(real)
    except OSError:
        pytest.skip("file symlinks are unavailable on this Windows runner")

    assert _error_code(settings) == "USER_SETTINGS_UNSAFE_PATH"
    assert real.read_bytes().count(FALSE_WIDGET_PAIR) == 1


def test_target_drift_before_publish_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from installer import patch_native_user_settings as module

    settings = tmp_path / "usersettings.dat"
    original = _settings_blob(FALSE_WIDGET_PAIR)
    settings.write_bytes(original)
    real_publish = module._publish_target

    def drift_then_publish(path: Path, payload: bytes, snapshot) -> None:
        path.write_bytes(_settings_blob(FALSE_WIDGET_PAIR, suffix=b"changed"))
        real_publish(path, payload, snapshot)

    monkeypatch.setattr(module, "_publish_target", drift_then_publish)

    assert _error_code(settings) == "USER_SETTINGS_TARGET_DRIFT"
    assert settings.read_bytes() != original
    assert settings.read_bytes().count(FALSE_WIDGET_PAIR) == 1


def test_replace_failure_never_changes_the_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from installer import patch_native_user_settings as module

    settings = tmp_path / "usersettings.dat"
    original = _settings_blob(FALSE_WIDGET_PAIR)
    settings.write_bytes(original)
    real_replace = module.os.replace

    def fail_target_replace(source, destination) -> None:
        if Path(destination) == settings:
            raise PermissionError("synthetic busy target")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_target_replace)

    assert _error_code(settings) == "USER_SETTINGS_REPLACE_FAILED"
    assert settings.read_bytes() == original
    assert settings.with_name("usersettings.dat.native-nav.orig").read_bytes() == original


def test_native_settings_helper_is_required_in_setup_and_patch_payloads() -> None:
    from installer.build_windows_setup import RELEASE_INSTALLER_FILES
    from installer.full_patch import PAYLOAD_REQUIRED_RELATIVE_FILES

    relative = "installer/patch_native_user_settings.py"
    assert relative in RELEASE_INSTALLER_FILES
    assert relative in PAYLOAD_REQUIRED_RELATIVE_FILES
