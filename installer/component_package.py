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
from pathlib import Path, PurePosixPath

from installer.component_update import ComponentUpdateError, _validate_relative_path
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
    top_level = Path(_git(source, "rev-parse", "--show-toplevel")).resolve()
    if top_level != source:
        raise ComponentPackageBuildError("UPDATE_SOURCE_NOT_TOPLEVEL")
    actual = _git(source, "rev-parse", "HEAD").lower()
    if actual != expected:
        raise ComponentPackageBuildError("UPDATE_SOURCE_COMMIT_MISMATCH")
    if _git(source, "status", "--porcelain=v1", "--untracked-files=no"):
        raise ComponentPackageBuildError("UPDATE_SOURCE_DIRTY")
    return actual


def _export_commit(source: Path, commit: str, destination: Path, archive_path: Path) -> None:
    try:
        with archive_path.open("wb") as stream:
            subprocess.run(
                ["git", "-C", str(source), "archive", "--format=zip", commit],
                check=True,
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        with zipfile.ZipFile(archive_path) as archive:
            files: set[str] = set()
            directories: set[str] = set()
            for info in archive.infolist():
                name = info.filename
                value = name[:-1] if info.is_dir() and name.endswith("/") else name
                try:
                    normalized = _validate_relative_path(value)
                except ComponentUpdateError as exc:
                    raise ComponentPackageBuildError(
                        "UPDATE_SOURCE_ARCHIVE_UNSAFE"
                    ) from exc
                relative = PurePosixPath(normalized)
                member_kind = stat.S_IFMT(info.external_attr >> 16)
                if (
                    not name
                    or "\\" in name
                    or (not info.is_dir() and member_kind not in {0, stat.S_IFREG})
                ):
                    raise ComponentPackageBuildError("UPDATE_SOURCE_ARCHIVE_UNSAFE")
                key = normalized.casefold()
                parent_keys = {
                    PurePosixPath(*relative.parts[:index]).as_posix().casefold()
                    for index in range(1, len(relative.parts))
                }
                if key in files or key in directories or parent_keys & files:
                    raise ComponentPackageBuildError("UPDATE_SOURCE_ARCHIVE_UNSAFE")
                directories.update(parent_keys)
                if info.is_dir():
                    directories.add(key)
                else:
                    files.add(key)
                target = destination.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ComponentPackageBuildError("UPDATE_SOURCE_ARCHIVE_UNSAFE")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
    except ComponentPackageBuildError:
        raise
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        zipfile.BadZipFile,
    ) as exc:
        raise ComponentPackageBuildError("UPDATE_SOURCE_ARCHIVE_FAILED") from exc


def _publish_outputs(staged: list[tuple[Path, Path]]) -> None:
    reserved: list[Path] = []
    try:
        for _source, destination in staged:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(descriptor)
            reserved.append(destination)
    except FileExistsError as exc:
        for path in reserved:
            path.unlink(missing_ok=True)
        raise ComponentPackageBuildError("UPDATE_OUTPUT_EXISTS") from exc
    except OSError as exc:
        for path in reserved:
            path.unlink(missing_ok=True)
        raise ComponentPackageBuildError("UPDATE_BUILD_FAILED") from exc
    try:
        for source, destination in staged:
            os.replace(source, destination)
    except OSError as exc:
        for path in reserved:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ComponentPackageBuildError("UPDATE_BUILD_FAILED") from exc


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

    staging: Path | None = None
    try:
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
        staging = Path(
            tempfile.mkdtemp(prefix=".olivia-update-build-", dir=package.parent)
        )
        exported_source = staging / "source"
        exported_source.mkdir()
        _export_commit(
            source_root,
            source_commit,
            exported_source,
            staging / "source.zip",
        )
        payload = staging / "payload"
        try:
            copy_project_payload(exported_source, payload)
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
        _publish_outputs(
            [
                (staged_manifest_sidecar, manifest_sidecar),
                (staged_package_sidecar, package_sidecar),
                (staged_package, package),
            ]
        )
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
    except (OSError, RuntimeError) as exc:
        raise ComponentPackageBuildError("UPDATE_BUILD_FAILED") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "ComponentPackageBuildError",
    "build_component_package",
]
