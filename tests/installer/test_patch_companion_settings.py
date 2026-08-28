from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

from original_client_settings_ui import (
    BOOTSTRAP_JAVASCRIPT,
    SETTINGS_UI_VERSION,
)
from patch_companion_settings import (
    BOOTSTRAP_MEMBER,
    CompanionSettingsPatchError,
    INDEX_MEMBER,
    MAIN_MODULE_MEMBER,
    PATCH_MARKER,
    patch_companion_settings,
    validate_api_base,
)


INDEX = """<!doctype html>
<html><head>
<script type="module" crossorigin src="./assets/main-917d29fc.js"></script>
<link rel="stylesheet" href="./assets/index.css">
</head><body><div id="app"></div></body></html>
"""


def _archive(
    path: Path,
    *,
    index: str = INDEX,
    main: bytes = b"synthetic-main-module",
    extra: dict[str, bytes] | None = None,
) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(INDEX_MEMBER, index)
        archive.writestr(MAIN_MODULE_MEMBER, main)
        archive.writestr("assets/index.css", b"body{display:block}")
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return path


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            info.filename: archive.read(info)
            for info in archive.infolist()
            if not info.is_dir()
        }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_patch_adds_original_settings_management_and_preserves_existing_assets(
    tmp_path: Path,
) -> None:
    path = _archive(tmp_path / "feapp.dat")
    before = _members(path)
    source_hash = _sha(path)

    result = patch_companion_settings(
        path,
        "http://127.0.0.1:8899",
        work_root=tmp_path,
    )

    after = _members(path)
    assert result["status"] == "PATCHED"
    assert result["ui_version"] == SETTINGS_UI_VERSION
    assert result["source_sha256"] == source_hash
    assert result["backup_name"] == "feapp.dat.companion.orig"
    assert _sha(tmp_path / result["backup_name"]) == source_hash
    assert set(after) == set(before) | {BOOTSTRAP_MEMBER}
    for name, value in before.items():
        if name != INDEX_MEMBER:
            assert after[name] == value

    index = after[INDEX_MEMBER].decode()
    bootstrap = after[BOOTSTRAP_MEMBER].decode()
    assert index.count(PATCH_MARKER) == 1
    assert 'data-api-base="http://127.0.0.1:8899/"' in index
    assert f'data-ui-version="{SETTINGS_UI_VERSION}"' in index
    for path_value in (
        "/toy/companion/status",
        "/toy/companion/memory",
        "/toy/companion/memory/correct",
        "/toy/companion/memory/delete",
        "/toy/companion/memory/pause",
        "/toy/companion/memory/resume",
        "/toy/letter/legacy/official-import",
    ):
        assert path_value in bootstrap
    for visible_text in (
        "长期记忆",
        "私人世界",
        "搜索长期记忆",
        "保存更正",
        "删除",
        "暂停长期记忆",
        "恢复长期记忆",
        "导入官方文字信件",
    ):
        assert visible_text in bootstrap
    for hidden_artifact in (
        "/toy/companion/private-world",
        "/toy/companion/private-world/candidates",
        "PRIVATE_WORLD_PATH",
        "CANDIDATES_PATH",
        "待确认的关系建议",
        "批准",
        "拒绝",
        "本地世界线",
        "approve",
        "reject",
        "encodeURIComponent",
    ):
        assert hidden_artifact not in bootstrap
    assert "MutationObserver" in bootstrap
    assert "replaceChildren" in bootstrap
    assert 'method: "GET"' in bootstrap
    assert 'method: "POST"' in bootstrap
    assert "X-Olivia-Companion-Action" in bootstrap
    assert "window.confirm" in bootstrap
    assert "crypto.randomUUID" in bootstrap
    assert "<iframe" not in bootstrap.casefold()
    assert "window.open" not in bootstrap
    assert "innerHTML" not in bootstrap
    assert 'method: "PUT"' not in bootstrap
    assert 'method: "DELETE"' not in bootstrap
    assert "http://" not in bootstrap
    without_provider_presets = bootstrap.replace(
        "https://api.deepseek.com", ""
    ).replace("https://opencode.ai/zen/go/v1", "")
    assert "https://" not in without_provider_presets
    assert "/toy/capabilities/mem0/import" not in bootstrap


def test_patch_supports_original_client_0_0_9_627_main_module(
    tmp_path: Path,
) -> None:
    main_member = "assets/main-31595bd3.js"
    index = INDEX.replace("main-917d29fc.js", "main-31595bd3.js")
    path = tmp_path / "feapp.dat"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(INDEX_MEMBER, index)
        archive.writestr(
            main_member,
            b'synthetic-0.0.9.627-main-module"hide-write":!1',
        )
        archive.writestr("assets/index.css", b"body{display:block}")

    result = patch_companion_settings(
        path,
        "http://127.0.0.1:8899",
        work_root=tmp_path,
    )

    after = _members(path)
    assert result["status"] == "PATCHED"
    assert after[main_member] == b'synthetic-0.0.9.627-main-module"hide-write":!1'
    assert PATCH_MARKER in after[INDEX_MEMBER].decode()
    assert BOOTSTRAP_MEMBER in after


def test_patch_is_idempotent_for_the_same_api_base(tmp_path: Path) -> None:
    path = _archive(tmp_path / "feapp.dat")
    first = patch_companion_settings(
        path,
        "http://localhost:8899",
        work_root=tmp_path,
    )
    first_hash = _sha(path)

    second = patch_companion_settings(
        path,
        "http://localhost:8899/",
        work_root=tmp_path,
    )

    assert first["status"] == "PATCHED"
    assert second["status"] == "ALREADY_PATCHED"
    assert second["ui_version"] == SETTINGS_UI_VERSION
    assert _sha(path) == first_hash
    assert _members(path)[INDEX_MEMBER].decode().count(PATCH_MARKER) == 1


def test_repository_owned_bootstrap_is_upgraded_without_touching_main_module(
    tmp_path: Path,
) -> None:
    legacy_tag = (
        '<script src="./assets/olivia-companion-settings.js" '
        'data-olivia-companion-settings="p03.original-settings-shell.v1" '
        'data-api-base="http://127.0.0.1:8899/"></script>'
    )
    legacy_index = INDEX.replace("</head>", legacy_tag + "</head>")
    path = _archive(
        tmp_path / "feapp.dat",
        index=legacy_index,
        extra={BOOTSTRAP_MEMBER: b"legacy repository-owned bootstrap"},
    )
    before_main = _members(path)[MAIN_MODULE_MEMBER]

    result = patch_companion_settings(
        path,
        "http://127.0.0.1:8899",
        work_root=tmp_path,
    )

    after = _members(path)
    assert result["status"] == "PATCHED"
    assert after[MAIN_MODULE_MEMBER] == before_main
    assert after[BOOTSTRAP_MEMBER].decode() == BOOTSTRAP_JAVASCRIPT
    index = after[INDEX_MEMBER].decode()
    assert f'data-ui-version="{SETTINGS_UI_VERSION}"' in index
    assert index.count(PATCH_MARKER) == 1


def test_repository_owned_bootstrap_upgrade_restores_0627_mailbox_write_access(
    tmp_path: Path,
) -> None:
    main_member = "assets/main-31595bd3.js"
    old_bootstrap = BOOTSTRAP_JAVASCRIPT.replace(
        '    document.querySelector(`[${ROOT_ATTR}]`)?.remove();\n',
        '    document.querySelector(`[${ROOT_ATTR}]`)?.remove();\n'
        '    document.querySelector(`[${DIALOG_ATTR}]`)?.remove();\n',
        1,
    )
    legacy_tag = (
        '<script src="./assets/olivia-companion-settings.js" '
        'data-olivia-companion-settings="p03.original-settings-shell.v1" '
        'data-ui-version="p03.original-settings-manage.v6" '
        'data-api-base="http://127.0.0.1:8899/"></script>'
    )
    index = INDEX.replace("main-917d29fc.js", "main-31595bd3.js")
    index = index.replace("</head>", legacy_tag + "</head>")
    path = tmp_path / "feapp.dat"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(INDEX_MEMBER, index)
        archive.writestr(main_member, b'prefix"hide-write":o(p)||!o(N3)suffix')
        archive.writestr(BOOTSTRAP_MEMBER, old_bootstrap)
        archive.writestr("assets/index.css", b"body{display:block}")

    result = patch_companion_settings(
        path,
        "http://127.0.0.1:8899",
        work_root=tmp_path,
    )

    after = _members(path)
    assert result["status"] == "PATCHED"
    assert after[BOOTSTRAP_MEMBER].decode() == BOOTSTRAP_JAVASCRIPT
    main = after[main_member].decode()
    assert '"hide-write":!1' in main
    assert '"hide-write":o(p)||!o(N3)' not in main


def test_0627_mailbox_write_repair_rejects_missing_anchor_without_mutation(
    tmp_path: Path,
) -> None:
    main_member = "assets/main-31595bd3.js"
    index = INDEX.replace("main-917d29fc.js", "main-31595bd3.js")
    path = tmp_path / "feapp.dat"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(INDEX_MEMBER, index)
        archive.writestr(main_member, b"unsupported-main")
        archive.writestr("assets/index.css", b"body{display:block}")
    before = path.read_bytes()

    with pytest.raises(CompanionSettingsPatchError) as error:
        patch_companion_settings(
            path,
            "http://127.0.0.1:8899",
            work_root=tmp_path,
        )

    assert error.value.code == "COMPANION_MAILBOX_WRITE_ANCHOR_INVALID"
    assert path.read_bytes() == before


def test_repatch_with_a_different_api_base_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
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


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://127.0.0.1:8899",
        "http://example.invalid:8899",
        "http://127.0.0.1",
        "http://user@127.0.0.1:8899",
        "http://127.0.0.1:8899/base",
        "http://127.0.0.1:8899?token=x",
        "http://127.0.0.1:8899/#fragment",
    ],
)
def test_api_base_requires_an_explicit_loopback_http_port(
    value: str | None,
) -> None:
    with pytest.raises(CompanionSettingsPatchError):
        validate_api_base(value)


def test_missing_or_duplicate_module_anchor_rolls_back(tmp_path: Path) -> None:
    cases = (
        INDEX.replace(
            '<script type="module" crossorigin src="./assets/main-917d29fc.js"></script>',
            "",
        ),
        INDEX.replace(
            '<script type="module" crossorigin src="./assets/main-917d29fc.js"></script>',
            '<script type="module" crossorigin src="./assets/main-917d29fc.js"></script>'
            * 2,
        ),
    )
    for number, index in enumerate(cases):
        path = _archive(tmp_path / f"case-{number}.dat", index=index)
        before = path.read_bytes()
        with pytest.raises(CompanionSettingsPatchError) as error:
            patch_companion_settings(
                path,
                "http://127.0.0.1:8899",
                work_root=tmp_path,
            )
        assert error.value.code == "COMPANION_MODULE_ANCHOR_INVALID"
        assert path.read_bytes() == before


def test_missing_main_module_and_incomplete_patch_are_rejected(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-main.dat"
    with zipfile.ZipFile(missing, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(INDEX_MEMBER, INDEX)
    with pytest.raises(CompanionSettingsPatchError) as missing_error:
        patch_companion_settings(
            missing,
            "http://127.0.0.1:8899",
            work_root=tmp_path,
        )
    assert missing_error.value.code == "COMPANION_MAIN_MODULE_MISSING"

    incomplete = _archive(
        tmp_path / "incomplete.dat",
        index=INDEX.replace(
            "</head>",
            '<script data-olivia-companion-settings="p03.original-settings-shell.v1" '
            'data-api-base="http://127.0.0.1:8899/"></script></head>',
        ),
    )
    with pytest.raises(CompanionSettingsPatchError) as incomplete_error:
        patch_companion_settings(
            incomplete,
            "http://127.0.0.1:8899",
            work_root=tmp_path,
        )
    assert incomplete_error.value.code == "COMPANION_PATCH_INCOMPLETE"


def test_unsafe_archive_member_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.dat"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(INDEX_MEMBER, INDEX)
        archive.writestr(MAIN_MODULE_MEMBER, b"main")
        archive.writestr("../outside.js", b"escape")

    with pytest.raises(CompanionSettingsPatchError) as error:
        patch_companion_settings(
            path,
            "http://127.0.0.1:8899",
            work_root=tmp_path,
        )

    assert error.value.code == "COMPANION_ARCHIVE_UNSAFE"


def test_existing_backup_is_never_overwritten(tmp_path: Path) -> None:
    path = _archive(tmp_path / "feapp.dat")
    backup = _archive(
        tmp_path / "feapp.dat.companion.orig",
        main=b"first-backup",
    )
    backup_before = backup.read_bytes()

    patch_companion_settings(
        path,
        "http://127.0.0.1:8899",
        work_root=tmp_path,
    )

    assert backup.read_bytes() == backup_before
