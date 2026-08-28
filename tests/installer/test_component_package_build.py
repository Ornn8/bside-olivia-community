from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from installer import __main__ as installer_cli
from installer.component_package import (
    ComponentPackageBuildError,
    build_component_package,
)
from installer.component_update import apply_component_update
from installer.full_patch import (
    PAYLOAD_REQUIRED_RELATIVE_FILES,
    PAYLOAD_REQUIRED_ROOT_FILES,
)


def _run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _clean_payload_repo(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    for relative in sorted(PAYLOAD_REQUIRED_ROOT_FILES):
        (source / relative).write_text(f"root:{relative}\n", encoding="utf-8")
    for relative in sorted(PAYLOAD_REQUIRED_RELATIVE_FILES):
        target = source.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"relative:{relative}\n", encoding="utf-8")
    for relative in (
        "installer/configure.py",
        "installer/start_local.py",
        "installer/uninstall.py",
    ):
        target = source.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"entrypoint:{relative}\n", encoding="utf-8")
    _run_git(source, "init")
    _run_git(source, "config", "user.email", "test@example.invalid")
    _run_git(source, "config", "user.name", "Olivia Test")
    _run_git(source, "add", ".")
    _run_git(source, "commit", "-m", "fixture")
    return source, _run_git(source, "rev-parse", "HEAD")


def _managed_installation(tmp_path: Path) -> Path:
    installation = tmp_path / "install"
    (installation / "local_backend").mkdir(parents=True)
    (installation / "local_backend" / "legacy.py").write_text(
        "legacy\n", encoding="utf-8"
    )
    (installation / ".olivia-full-patch.json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.full-patch.install.v2",
                "owned_root": str(installation.resolve()),
            }
        ),
        encoding="utf-8",
    )
    return installation


def test_builds_release_ready_component_package_that_the_updater_accepts(
    tmp_path: Path,
) -> None:
    source, commit = _clean_payload_repo(tmp_path)
    package = tmp_path / "olivia-local-backend-0.1.1.oliviapatch"

    result = build_component_package(
        source,
        package,
        version="0.1.1",
        expected_source_commit=commit,
    )

    assert result["status"] == "BUILT"
    assert result["source_commit"] == commit
    assert result["manifest_sha256"] == Path(
        f"{package}.manifest.sha256"
    ).read_text(encoding="ascii").strip()
    assert result["package_sha256"] == Path(f"{package}.sha256").read_text(
        encoding="ascii"
    ).strip()
    with zipfile.ZipFile(package) as archive:
        assert archive.namelist()[0] == "manifest.json"
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == "0.1.1"
        assert {item["path"] for item in manifest["files"]} >= {
            "installer/start_local.py",
            "installer/configure.py",
            "installer/uninstall.py",
        }

    installation = _managed_installation(tmp_path)
    applied = apply_component_update(
        installation,
        package,
        expected_manifest_sha256=result["manifest_sha256"],
    )
    assert applied == {
        "status": "APPLIED",
        "component": "local_backend",
        "version": "0.1.1",
    }

    second = tmp_path / "second.oliviapatch"
    second_result = build_component_package(
        source,
        second,
        version="0.1.1",
        expected_source_commit=commit,
    )
    assert second_result["package_sha256"] == result["package_sha256"]
    assert second.read_bytes() == package.read_bytes()


def test_rejects_dirty_or_wrong_source_before_writing_outputs(tmp_path: Path) -> None:
    source, commit = _clean_payload_repo(tmp_path)
    package = tmp_path / "update.oliviapatch"

    with pytest.raises(ComponentPackageBuildError, match="UPDATE_SOURCE_COMMIT_MISMATCH"):
        build_component_package(
            source,
            package,
            version="0.1.1",
            expected_source_commit="0" * 40,
        )
    assert not package.exists()

    (source / "local_server.py").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ComponentPackageBuildError, match="UPDATE_SOURCE_DIRTY"):
        build_component_package(
            source,
            package,
            version="0.1.1",
            expected_source_commit=commit,
        )
    assert not package.exists()


def test_cli_builds_component_package(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source, commit = _clean_payload_repo(tmp_path)
    package = tmp_path / "cli.oliviapatch"

    exit_code = installer_cli.main(
        [
            "build-update",
            "--source",
            str(source),
            "--output",
            str(package),
            "--version",
            "0.1.1",
            "--source-commit",
            commit,
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "BUILT"
    assert package.is_file()
