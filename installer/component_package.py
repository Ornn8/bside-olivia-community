"""Build one verified local-backend component update package."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

from installer.full_patch import PatchInstallError, copy_project_payload


PACKAGE_SCHEMA = "olivia.component-package.v1"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")


class ComponentPackageBuildError(RuntimeError):
    """Stable release-builder failure code."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(source: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ComponentPackageBuildError("UPDATE_SOURCE_GIT_INVALID") from exc
    return result.stdout.strip()


def _verify_source(source: Path, expected_source_commit: str) -> str:
    expected = expected_source_commit.lower()
    if not _COMMIT_RE.fullmatch(expected):
        raise ComponentPackageBuildError("UPDATE_SOURCE_COMMIT_INVALID")
    actual = _git(source, "rev-parse", "HEAD").lower()
    if actual != expected:
        raise ComponentPackageBuildError("UPDATE_SOURCE_COMMIT_MISMATCH")
    if _git(source, "status", "--porcelain=v1", "--untracked-files=no"):
        raise ComponentPackageBuildError("UPDATE_SOURCE_DIRTY")
    return actual


def _payload_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                raise ComponentPackageBuildError("UPDATE_PAYLOAD_UNSAFE")
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _write_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    archive.writestr(info, content, compresslevel=9)


def build_component_package(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    version: str,
    expected_source_commit: str,
) -> dict[str, object]:
    """Build a deterministic package plus independent manifest/package digests."""

    source_root = Path(source).expanduser().resolve()
    package = Path(output).expanduser().resolve()
    if not source_root.is_dir():
        raise ComponentPackageBuildError("UPDATE_SOURCE_INVALID")
    if package.suffix.lower() != ".oliviapatch":
        raise ComponentPackageBuildError("UPDATE_OUTPUT_INVALID")
    if not _VERSION_RE.fullmatch(version):
        raise ComponentPackageBuildError("UPDATE_VERSION_INVALID")
    manifest_sidecar = Path(f"{package}.manifest.sha256")
    package_sidecar = Path(f"{package}.sha256")
    if any(path.exists() for path in (package, manifest_sidecar, package_sidecar)):
        raise ComponentPackageBuildError("UPDATE_OUTPUT_EXISTS")
    source_commit = _verify_source(source_root, expected_source_commit)
    package.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=".olivia-update-build-", dir=package.parent))
    try:
        payload = staging / "payload"
        try:
            copy_project_payload(source_root, payload)
        except PatchInstallError as exc:
            raise ComponentPackageBuildError(str(exc)) from exc
        _verify_source(source_root, source_commit)
        files = _payload_files(payload)
        manifest = {
            "schema_version": PACKAGE_SCHEMA,
            "component": "local_backend",
            "version": version,
            "files": [
                {
                    "path": path.relative_to(payload).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for path in files
            ],
        }
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        staged_package = staging / package.name
        with zipfile.ZipFile(staged_package, "w") as archive:
            _write_member(archive, "manifest.json", manifest_bytes)
            for path in files:
                relative = path.relative_to(payload).as_posix()
                _write_member(archive, f"payload/{relative}", path.read_bytes())
        package_sha256 = _sha256(staged_package)
        staged_manifest_sidecar = staging / manifest_sidecar.name
        staged_package_sidecar = staging / package_sidecar.name
        staged_manifest_sidecar.write_text(manifest_sha256 + "\n", encoding="ascii")
        staged_package_sidecar.write_text(package_sha256 + "\n", encoding="ascii")
        os.replace(staged_manifest_sidecar, manifest_sidecar)
        os.replace(staged_package_sidecar, package_sidecar)
        os.replace(staged_package, package)
        return {
            "status": "BUILT",
            "component": "local_backend",
            "version": version,
            "source_commit": source_commit,
            "file_count": len(files),
            "package": str(package),
            "manifest_sha256": manifest_sha256,
            "package_sha256": package_sha256,
        }
    except ComponentPackageBuildError:
        raise
    except OSError as exc:
        raise ComponentPackageBuildError("UPDATE_BUILD_FAILED") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "ComponentPackageBuildError",
    "build_component_package",
]
