from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from patch_companion_settings import (
    BOOTSTRAP_MEMBER,
    CompanionSettingsPatchError,
    INDEX_MEMBER,
    MAIN_MODULE_MEMBER,
    patch_companion_settings,
)
from runtime.personal_chat.settings_patch import PERSONAL_CHAT_MARKER


INDEX = """<!doctype html>
<html><head>
<script type="module" crossorigin src="./assets/main-917d29fc.js"></script>
<link rel="stylesheet" href="./assets/index.css">
</head><body><div id="app"></div></body></html>
"""


def _archive(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(INDEX_MEMBER, INDEX)
        archive.writestr(MAIN_MODULE_MEMBER, b"synthetic-main-module")
        archive.writestr("assets/index.css", b"body{display:block}")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_personal_chat_ui_is_separate_from_legacy_settings_bootstrap(tmp_path: Path) -> None:
    path = _archive(tmp_path / "feapp.dat")

    first = patch_companion_settings(
        path,
        "http://127.0.0.1:8899",
        work_root=tmp_path,
    )
    first_hash = _sha(path)

    with zipfile.ZipFile(path) as archive:
        bootstrap = archive.read(BOOTSTRAP_MEMBER).decode("utf-8")
        index = archive.read(INDEX_MEMBER).decode("utf-8")
        names = {info.filename for info in archive.infolist() if not info.is_dir()}

    assert first["status"] == "PATCHED"
    assert bootstrap == BOOTSTRAP_JAVASCRIPT
    assert names == {INDEX_MEMBER, MAIN_MODULE_MEMBER, "assets/index.css", BOOTSTRAP_MEMBER}
    assert index.count(PERSONAL_CHAT_MARKER) == 1
    assert "/toy/personal-chat/setup/status" in index
    assert "/toy/personal-chat/setup/wechat/start" in index
    assert "/toy/personal-chat/setup/qq/configure" in index
    assert "QQ / 微信聊天" in index

    second = patch_companion_settings(
        path,
        "http://127.0.0.1:8899/",
        work_root=tmp_path,
    )
    assert second["status"] == "ALREADY_PATCHED"
    assert _sha(path) == first_hash


def test_personal_chat_inline_patch_rolls_back_on_api_base_mismatch(tmp_path: Path) -> None:
    path = _archive(tmp_path / "feapp.dat")
    patch_companion_settings(
        path,
        "http://127.0.0.1:8899",
        work_root=tmp_path,
    )
    before = path.read_bytes()

    with pytest.raises(CompanionSettingsPatchError) as error:
        patch_companion_settings(
            path,
            "http://127.0.0.1:8900",
            work_root=tmp_path,
        )

    assert error.value.code == "COMPANION_API_BASE_MISMATCH"
    assert path.read_bytes() == before
