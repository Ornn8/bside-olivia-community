from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

from patch_webplayer import (
    BOOTSTRAP_MEMBER,
    PATCH_MARKER,
    WebPlayerPatchError,
    patch_webplayer,
)


ENTRY = "assets/main-fixture.js"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive(path: Path, *, html: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    index = html or (
        '<!doctype html><html><head><meta charset="UTF-8">'
        '<script type="module" crossorigin src="./assets/main-fixture.js"></script>'
        '<link rel="stylesheet" href="./assets/index.css">'
        '</head><body><div id="app"></div></body></html>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", index)
        archive.writestr(ENTRY, 'import "./vendor.js"; window.originalPlayerStarted = true;')
        archive.writestr("assets/vendor.js", "export const fixture = true;")
        archive.writestr("assets/index.css", "html,body{margin:0}")
    return path


def _read(path: Path, member: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(member).decode("utf-8")


def test_patch_preserves_original_module_and_adds_loopback_only_bootstrap(
    tmp_path: Path,
) -> None:
    player = _archive(tmp_path / "webplayer.dat")
    original_archive = player.read_bytes()
    original_module = _read(player, ENTRY)

    result = patch_webplayer(player)

    assert result["status"] == "PATCHED"
    assert result["source_sha256"] == hashlib.sha256(original_archive).hexdigest()
    assert result["backup_sha256"] == hashlib.sha256(original_archive).hexdigest()
    assert result["patched_sha256"] == _sha256(player)
    assert Path(str(player) + ".orig").read_bytes() == original_archive

    html = _read(player, "index.html")
    bootstrap = _read(player, BOOTSTRAP_MEMBER)
    assert html.count(PATCH_MARKER) == 1
    assert 'data-original-module="./assets/main-fixture.js"' in html
    assert '<script type="module" crossorigin src="./assets/main-fixture.js"></script>' not in html
    assert _read(player, ENTRY) == original_module

    assert 'params.get("uid")' in bootstrap
    assert 'url.protocol !== "http:"' in bootstrap
    assert 'url.hostname === "127.0.0.1"' in bootstrap
    assert 'url.hostname === "localhost"' in bootstrap
    assert 'url.pathname.startsWith("/toy/media/")' in bootstrap
    assert 'url.pathname.startsWith("/media/")' in bootstrap
    assert "url.username" in bootstrap
    assert "url.password" in bootstrap
    assert "url.hash" in bootstrap
    assert "loadOriginal();" in bootstrap
    assert 'document.createElement("video")' in bootstrap
    assert "https://" not in bootstrap
    assert "fetch(" not in bootstrap
    assert "XMLHttpRequest" not in bootstrap


def test_patch_is_idempotent_and_keeps_the_original_backup(tmp_path: Path) -> None:
    player = _archive(tmp_path / "webplayer.dat")
    original = player.read_bytes()

    first = patch_webplayer(player)
    first_patched = player.read_bytes()
    second = patch_webplayer(player)

    assert first["status"] == "PATCHED"
    assert second["status"] == "ALREADY_PATCHED"
    assert player.read_bytes() == first_patched
    assert Path(str(player) + ".orig").read_bytes() == original
    assert second["backup_sha256"] == hashlib.sha256(original).hexdigest()


def test_patch_rejects_missing_or_ambiguous_module_anchor_without_mutation(
    tmp_path: Path,
) -> None:
    cases = {
        "missing": "<!doctype html><html><body></body></html>",
        "ambiguous": (
            '<script type="module" src="./assets/main-fixture.js"></script>'
            '<script type="module" src="./assets/vendor.js"></script>'
        ),
    }
    for name, html in cases.items():
        player = _archive(tmp_path / name / "webplayer.dat", html=html)
        original = player.read_bytes()
        with pytest.raises(WebPlayerPatchError) as error:
            patch_webplayer(player)
        assert error.value.code == "WEBPLAYER_MODULE_ANCHOR_INVALID"
        assert player.read_bytes() == original


def test_patch_rejects_unsafe_archive_member_and_does_not_extract_it(
    tmp_path: Path,
) -> None:
    player = tmp_path / "webplayer.dat"
    with zipfile.ZipFile(player, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", '<script type="module" src="./assets/main.js"></script>')
        archive.writestr("assets/main.js", "fixture")
        archive.writestr("../outside.js", "escape")

    with pytest.raises(WebPlayerPatchError) as error:
        patch_webplayer(player)
    assert error.value.code == "WEBPLAYER_ARCHIVE_UNSAFE"
    assert not (tmp_path.parent / "outside.js").exists()


def test_patch_rejects_incomplete_existing_marker_and_restores_input(
    tmp_path: Path,
) -> None:
    player = _archive(
        tmp_path / "webplayer.dat",
        html=(
            '<script src="./assets/olivia-local-media-bootstrap.js" '
            f'{PATCH_MARKER}="p03.webplayer-local-media.v1" '
            'data-original-module="./assets/main-fixture.js"></script>'
        ),
    )
    original = player.read_bytes()

    with pytest.raises(WebPlayerPatchError) as error:
        patch_webplayer(player)

    assert error.value.code == "WEBPLAYER_PATCH_INCOMPLETE"
    assert player.read_bytes() == original


def test_patch_requires_existing_archive_and_work_root(tmp_path: Path) -> None:
    with pytest.raises(WebPlayerPatchError) as missing:
        patch_webplayer(tmp_path / "missing.dat")
    assert missing.value.code == "WEBPLAYER_ARCHIVE_NOT_FOUND"

    player = _archive(tmp_path / "webplayer.dat")
    with pytest.raises(WebPlayerPatchError) as work_root:
        patch_webplayer(player, work_root=tmp_path / "missing-root")
    assert work_root.value.code == "WEBPLAYER_WORK_ROOT_NOT_FOUND"
