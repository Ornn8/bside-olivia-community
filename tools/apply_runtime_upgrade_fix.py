"""One-shot implementation script for the reviewed runtime-upgrade blocker.

The accompanying workflow runs this script, validates the resulting source,
then removes both the script and workflow before committing the final tree.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FULL_PATCH = ROOT / "installer" / "full_patch.py"
TEST_FILE = ROOT / "tests" / "installer" / "test_full_patch_runtime_upgrade.py"


def replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise RuntimeError(f"RUNTIME_UPGRADE_{label}_ANCHOR_INVALID")
    return value.replace(old, new, 1)


def patch_full_patch() -> None:
    source = FULL_PATCH.read_text(encoding="utf-8")
    source = replace_once(
        source,
        "import re\nimport shutil\nfrom pathlib import Path\n",
        "import re\nimport shutil\nimport uuid\nfrom pathlib import Path\n",
        "IMPORT",
    )
    source = replace_once(
        source,
        'MARKER_NAME = ".olivia-full-patch.json"\nOWNED_PATHS = (\n',
        'MARKER_NAME = ".olivia-full-patch.json"\n'
        'RUNTIME_PAYLOAD_SCHEMA = "olivia.runtime-payload.v1"\n'
        '_RUNTIME_REPLACEMENTS = (\n'
        '    "local_backend",\n'
        '    "START.cmd",\n'
        '    "CONFIGURE.cmd",\n'
        '    "UNINSTALL.cmd",\n'
        ')\n'
        'OWNED_PATHS = (\n',
        "CONSTANTS",
    )

    old_marker_block = '''def _read_marker(path: Path) -> dict[str, Any]:
    marker = _load_json(path, "PATCH_MARKER_INVALID")
    if (
        marker.get("schema_version") != "olivia.full-patch.install.v2"
        or marker.get("owned_root") != str(path.parent.resolve())
    ):
        raise PatchInstallError("PATCH_MARKER_INVALID")
    return marker


def _marker_matches_current_install(
    marker: dict[str, Any],
    manifest: dict[str, Any],
) -> bool:
    return (
        marker.get("official_feapp_sha256")
        == manifest["feapp_sha256"]
        and marker.get("official_webplayer_sha256")
        == manifest["webplayer_sha256"]
        and marker.get("original_client_only") is True
        and marker.get("companion_settings_embedded") is True
        and marker.get("webplayer_local_media") is True
    )
'''
    new_marker_block = '''def _read_marker(path: Path) -> dict[str, Any]:
    marker = _load_json(path, "PATCH_MARKER_INVALID")
    if (
        marker.get("schema_version") != "olivia.full-patch.install.v2"
        or marker.get("owned_root") != str(path.parent.resolve())
    ):
        raise PatchInstallError("PATCH_MARKER_INVALID")
    return marker


def _marker_matches_current_install(
    marker: dict[str, Any],
    manifest: dict[str, Any],
) -> bool:
    """Prove that an existing directory is one of our controlled installs."""

    return (
        marker.get("official_feapp_sha256")
        == manifest["feapp_sha256"]
        and marker.get("official_webplayer_sha256")
        == manifest["webplayer_sha256"]
        and marker.get("original_client_only") is True
        and marker.get("companion_settings_embedded") is True
        and marker.get("webplayer_local_media") is True
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _runtime_payload_files(root: Path) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    ignored_parts = {"__pycache__", ".pytest_cache", ".git", ".evidence"}
    for name in _RUNTIME_REPLACEMENTS:
        path = root / name
        if not path.exists() or path.is_symlink():
            raise PatchInstallError("PATCH_PAYLOAD_INCOMPLETE")
        if path.is_file():
            files.append((path.relative_to(root), path))
            continue
        if not path.is_dir():
            raise PatchInstallError("PATCH_PAYLOAD_INCOMPLETE")
        for candidate in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
            relative = candidate.relative_to(root)
            if any(part in ignored_parts for part in relative.parts):
                continue
            if candidate.is_symlink():
                raise PatchInstallError("PATCH_PAYLOAD_INCOMPLETE")
            if candidate.is_file() and candidate.suffix.lower() not in {".pyc", ".pyo"}:
                files.append((relative, candidate))
    if not files:
        raise PatchInstallError("PATCH_PAYLOAD_INCOMPLETE")
    return files


def _runtime_payload_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        for relative, path in _runtime_payload_files(root):
            encoded = relative.as_posix().encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
    except OSError as exc:
        raise PatchInstallError("PATCH_PAYLOAD_DIGEST_FAILED") from exc
    return digest.hexdigest()


def _prepare_runtime_payload(
    payload: Path,
    staging: Path,
    *,
    port: int,
) -> tuple[list[str], str]:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    copied = copy_project_payload(payload, staging / "local_backend")
    _write_start_scripts(staging, port)
    return copied, _runtime_payload_sha256(staging)


def _write_marker(path: Path, marker: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            marker,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _upgrade_runtime_payload(
    target: Path,
    staging: Path,
    marker: dict[str, Any],
) -> None:
    """Atomically replace managed runtime files while preserving user data."""

    _write_marker(staging / MARKER_NAME, marker)
    backup = target.parent / f".{target.name}.upgrade-backup-{uuid.uuid4().hex}"
    backup.mkdir(parents=True)
    moved_old: list[str] = []
    moved_new: list[str] = []
    rollback_failed = False
    try:
        for name in (*_RUNTIME_REPLACEMENTS, MARKER_NAME):
            source = staging / name
            destination = target / name
            if not source.exists():
                raise PatchInstallError("PATCH_PAYLOAD_INCOMPLETE")
            if destination.exists():
                archived = backup / name
                archived.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, archived)
                moved_old.append(name)
            os.replace(source, destination)
            moved_new.append(name)
    except Exception as exc:
        for name in reversed(moved_new):
            try:
                _remove_path(target / name)
            except OSError:
                rollback_failed = True
        for name in reversed(moved_old):
            archived = backup / name
            destination = target / name
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archived, destination)
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise PatchInstallError(
                "PATCH_RUNTIME_UPGRADE_ROLLBACK_FAILED"
            ) from exc
        raise PatchInstallError("PATCH_RUNTIME_UPGRADE_FAILED") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if not rollback_failed:
            shutil.rmtree(backup, ignore_errors=True)
'''
    source = replace_once(
        source,
        old_marker_block,
        new_marker_block,
        "MARKER_BLOCK",
    )

    old_existing = '''    marker_path = target / MARKER_NAME
    if target.exists():
        if marker_path.is_file():
            marker = _read_marker(marker_path)
            if _marker_matches_current_install(marker, manifest):
                return {"status": "ALREADY_INSTALLED", **marker}
        raise PatchInstallError("INSTALL_ROOT_ALREADY_EXISTS")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target
'''
    new_existing = '''    if not 1 <= port <= 65535:
        raise PatchInstallError("INVALID_PORT")
    marker_path = target / MARKER_NAME
    if target.exists():
        if not marker_path.is_file():
            raise PatchInstallError("INSTALL_ROOT_ALREADY_EXISTS")
        marker = _read_marker(marker_path)
        if not _marker_matches_current_install(marker, manifest):
            raise PatchInstallError("INSTALL_ROOT_ALREADY_EXISTS")

        staging = target.parent / (
            f".{target.name}.runtime-{uuid.uuid4().hex}"
        )
        try:
            copied, payload_sha256 = _prepare_runtime_payload(
                payload,
                staging,
                port=port,
            )
            try:
                installed_sha256 = _runtime_payload_sha256(target)
            except PatchInstallError:
                installed_sha256 = None
            if (
                marker.get("runtime_payload_schema")
                == RUNTIME_PAYLOAD_SCHEMA
                and marker.get("runtime_payload_sha256")
                == payload_sha256
                and installed_sha256 == payload_sha256
            ):
                shutil.rmtree(staging, ignore_errors=True)
                return {"status": "ALREADY_INSTALLED", **marker}

            upgraded = dict(marker)
            upgraded.update(
                {
                    "official_source": str(source),
                    "port": port,
                    "payload_entries": copied,
                    "runtime_payload_schema": RUNTIME_PAYLOAD_SCHEMA,
                    "runtime_payload_sha256": payload_sha256,
                    "owned_paths": list(OWNED_PATHS),
                    "preserved_paths": list(PRESERVED_PATHS),
                    "live_status": manifest["live_status"],
                    "media_status": manifest["media_status"],
                }
            )
            _upgrade_runtime_payload(target, staging, upgraded)
            return {"status": "UPGRADED", **upgraded}
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target
'''
    source = replace_once(
        source,
        old_existing,
        new_existing,
        "EXISTING_INSTALL",
    )

    source = replace_once(
        source,
        '''        if not 1 <= port <= 65535:
            raise PatchInstallError("INVALID_PORT")

        base_http = f"http://127.0.0.1:{port}"
''',
        '''        base_http = f"http://127.0.0.1:{port}"
''',
        "PORT_VALIDATION",
    )
    source = replace_once(
        source,
        '''        _write_start_scripts(staging, port)
        (staging / "data").mkdir()
        marker = {
''',
        '''        _write_start_scripts(staging, port)
        runtime_payload_sha256 = _runtime_payload_sha256(staging)
        (staging / "data").mkdir()
        marker = {
''',
        "FRESH_DIGEST",
    )
    source = replace_once(
        source,
        '''            "webplayer_local_media": True,
            "port": port,
''',
        '''            "webplayer_local_media": True,
            "runtime_payload_schema": RUNTIME_PAYLOAD_SCHEMA,
            "runtime_payload_sha256": runtime_payload_sha256,
            "port": port,
''',
        "FRESH_MARKER",
    )
    source = replace_once(
        source,
        '''        (staging / MARKER_NAME).write_text(
            json.dumps(
                marker,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
''',
        '''        _write_marker(staging / MARKER_NAME, marker)
''',
        "MARKER_WRITE",
    )
    FULL_PATCH.write_text(source, encoding="utf-8")


def write_tests() -> None:
    TEST_FILE.write_text(
        '''from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from installer import full_patch


BASE_SHA = "804943526b531e3b190a4adc947c2c18218866f6"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _official_and_manifest(tmp_path: Path) -> tuple[Path, Path]:
    official = tmp_path / "official"
    resources = official / "0.0.9.615" / "resources"
    resources.mkdir(parents=True)
    (official / "launcher.exe").write_bytes(b"official launcher")
    (official / "0.0.9.615" / "Olivia.exe").write_bytes(b"official client")
    feapp = resources / "feapp.dat"
    webplayer = resources / "webplayer.dat"
    feapp.write_bytes(b"official feapp")
    webplayer.write_bytes(b"official webplayer")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "olivia.full-patch.v2",
                "steam_app_id": "4532590",
                "client_version": "0.0.9.615",
                "feapp_sha256": _sha256(feapp),
                "webplayer_sha256": _sha256(webplayer),
                "patch_mode": "isolated-copy",
                "live_status": "UNAVAILABLE_PAUSED",
                "media_status": "ORIGINAL_WEBPLAYER_LOCAL_VIDEO",
            }
        ),
        encoding="utf-8",
    )
    return official, manifest


def _legacy_install(
    tmp_path: Path,
    official: Path,
    manifest_path: Path,
) -> Path:
    target = tmp_path / "install"
    target.mkdir()
    app = target / "app" / "0.0.9.615"
    app.mkdir(parents=True)
    (app / "Olivia.exe").write_bytes(b"patched client sentinel")
    (target / "data").mkdir()
    (target / "data" / "memory.sqlite3").write_bytes(b"user archive data")
    (target / "logs").mkdir()
    (target / "logs" / "keep.log").write_text("keep", encoding="utf-8")
    (target / "third-party").mkdir()
    (target / "third-party" / "keep.txt").write_text("keep", encoding="utf-8")

    backend = target / "local_backend" / "installer"
    backend.mkdir(parents=True)
    (backend / "start_local.py").write_text(
        "# BASE " + BASE_SHA + "\nOLIVIA_MEMORY_ROOT = 'archive-only'\n",
        encoding="utf-8",
    )
    for name in ("START.cmd", "CONFIGURE.cmd", "UNINSTALL.cmd"):
        (target / name).write_text("old " + name, encoding="utf-8")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    marker = {
        "schema_version": "olivia.full-patch.install.v2",
        "steam_app_id": "4532590",
        "client_version": "0.0.9.615",
        "official_source": str(official.resolve()),
        "owned_root": str(target.resolve()),
        "official_feapp_sha256": manifest["feapp_sha256"],
        "patched_feapp_sha256": "0" * 64,
        "backup_feapp_sha256": "1" * 64,
        "companion_backup_feapp_sha256": "2" * 64,
        "companion_settings_status": "PATCHED",
        "companion_settings_ui_version": "legacy",
        "official_webplayer_sha256": manifest["webplayer_sha256"],
        "patched_webplayer_sha256": "3" * 64,
        "backup_webplayer_sha256": "4" * 64,
        "webplayer_patch_status": "PATCHED",
        "original_client_only": True,
        "companion_settings_embedded": True,
        "webplayer_local_media": True,
        "port": 8899,
        "owned_paths": list(full_patch.OWNED_PATHS),
        "preserved_paths": list(full_patch.PRESERVED_PATHS),
        "runtime_entries": ["launcher.exe", "0.0.9.615/"],
        "payload_entries": ["legacy"],
        "live_status": manifest["live_status"],
        "media_status": manifest["media_status"],
    }
    (target / full_patch.MARKER_NAME).write_text(
        json.dumps(marker, sort_keys=True),
        encoding="utf-8",
    )
    return target


def test_exact_base_style_install_upgrades_runtime_without_deleting_user_data(
    tmp_path: Path,
) -> None:
    official, manifest = _official_and_manifest(tmp_path)
    target = _legacy_install(tmp_path, official, manifest)
    repo_root = Path(__file__).parents[2]

    result = full_patch.install_full_patch(
        official,
        target,
        repo_root,
        manifest,
    )

    assert result["status"] == "UPGRADED"
    installed_start = (
        target / "local_backend" / "installer" / "start_local.py"
    ).read_text(encoding="utf-8")
    assert "OLIVIA_MEMORY_ENABLED" in installed_start
    assert "OLIVIA_CONVERSATION_MEMORY_ROOT" in installed_start
    assert (target / "app" / "0.0.9.615" / "Olivia.exe").read_bytes() == b"patched client sentinel"
    assert (target / "data" / "memory.sqlite3").read_bytes() == b"user archive data"
    assert (target / "logs" / "keep.log").read_text(encoding="utf-8") == "keep"
    assert (target / "third-party" / "keep.txt").read_text(encoding="utf-8") == "keep"

    upgraded_marker = json.loads(
        (target / full_patch.MARKER_NAME).read_text(encoding="utf-8")
    )
    assert upgraded_marker["runtime_payload_schema"] == full_patch.RUNTIME_PAYLOAD_SCHEMA
    assert len(upgraded_marker["runtime_payload_sha256"]) == 64

    repeated = full_patch.install_full_patch(
        official,
        target,
        repo_root,
        manifest,
    )
    assert repeated["status"] == "ALREADY_INSTALLED"


def test_runtime_upgrade_rolls_back_all_managed_files_on_swap_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    official, manifest = _official_and_manifest(tmp_path)
    target = _legacy_install(tmp_path, official, manifest)
    repo_root = Path(__file__).parents[2]
    old_marker = (target / full_patch.MARKER_NAME).read_bytes()
    old_start = (target / "START.cmd").read_bytes()
    old_backend = (
        target / "local_backend" / "installer" / "start_local.py"
    ).read_bytes()

    real_replace = os.replace
    failed = False

    def fail_once(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        nonlocal failed
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not failed
            and ".runtime-" in source_path.as_posix()
            and source_path.name == "START.cmd"
            and destination_path == target / "START.cmd"
        ):
            failed = True
            raise OSError("synthetic swap failure")
        real_replace(source, destination)

    monkeypatch.setattr(full_patch.os, "replace", fail_once)

    with pytest.raises(
        full_patch.PatchInstallError,
        match="PATCH_RUNTIME_UPGRADE_FAILED",
    ):
        full_patch.install_full_patch(
            official,
            target,
            repo_root,
            manifest,
        )

    assert (target / full_patch.MARKER_NAME).read_bytes() == old_marker
    assert (target / "START.cmd").read_bytes() == old_start
    assert (
        target / "local_backend" / "installer" / "start_local.py"
    ).read_bytes() == old_backend
    assert (target / "data" / "memory.sqlite3").read_bytes() == b"user archive data"
    assert (target / "logs" / "keep.log").read_text(encoding="utf-8") == "keep"
    assert (target / "third-party" / "keep.txt").read_text(encoding="utf-8") == "keep"
''',
        encoding="utf-8",
    )


def main() -> None:
    patch_full_patch()
    write_tests()


if __name__ == "__main__":
    main()
