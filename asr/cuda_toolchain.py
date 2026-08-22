"""Manifest-driven, portable CUDA 13.3 assembly for the B05 build boundary.

This module never downloads anything.  It verifies NVIDIA redistributable
archives supplied through an explicit D:/ or F:/ transfer root, assembles an
owned toolchain under D:/ or F:/, and returns diagnostics suitable for the
management CLI.  A ready toolchain is only build-ready; it is not native ASR
acceptance evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin

from .errors import AsrError


CUDA_REDIST_MANIFEST_URL = (
    "https://developer.download.nvidia.com/compute/cuda/redist/redistrib_13.3.0.json"
)
CUDA_REDIST_BASE_URL = "https://developer.download.nvidia.com/compute/cuda/redist/"
CUDA_REDIST_LABEL = "13.3.0"
CUDA_REDIST_PRODUCT = "cuda"
CUDA_REDIST_PLATFORM = "windows-x86_64"
CUDA_REDIST_MANIFEST_BYTES = 47431
CUDA_REDIST_MANIFEST_SHA256 = "507EDDAAB1360336BC0FE17B77552E0B7DFE1E74DA888671C3A2F5FAD7775DB1"
CUDA_WINDOWS_BUILD_COMPONENTS = (
    "cccl",
    "cuda_crt",
    "cuda_ctadvisor",
    "cuda_cudart",
    "cuda_nvcc",
    "libcublas",
    "libnvfatbin",
    "libnvjitlink",
    "libnvptxcompiler",
    "libnvvm",
)
CUDA_TOOLCHAIN_OWNER = "b05-streaming-asr"
CUDA_TOOLCHAIN_MARKER = ".b05-cuda-toolchain.json"


@dataclass(frozen=True, slots=True)
class CudaPackage:
    """One pinned NVIDIA redistributable archive."""

    package: str
    name: str
    version: str
    license: str
    license_path: str
    relative_path: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "name": self.name,
            "version": self.version,
            "license": self.license,
            "license_path": self.license_path,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size": self.size,
        }


def _external_root(path: Path | str, label: str) -> Path:
    root = Path(path)
    if not root.is_absolute() or root.drive.upper() not in {"D:", "F:"}:
        raise AsrError(
            "ASR_CONFIG_INVALID",
            f"{label} must be an absolute D:/ or F:/ path",
            {"path": str(root), "label": label},
        )
    return root


def _manifest_error(code: str, reason: str, **details: Any) -> AsrError:
    return AsrError(code, reason, details)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path | str, *, strict: bool = True) -> dict[str, Any]:
    """Read and validate a local NVIDIA redistributable manifest.

    ``strict=False`` is intentionally available for small offline unit-test
    fixtures; production plans use the pinned size and SHA-256 by default.
    """

    manifest_path = _external_root(path, "CUDA redistributable manifest")
    if not manifest_path.is_file():
        raise _manifest_error(
            "ASR_TOOLCHAIN_MISSING", "CUDA redistributable manifest is missing", path=str(manifest_path)
        )
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise _manifest_error(
            "ASR_TOOLCHAIN_MISSING",
            "CUDA redistributable manifest cannot be read",
            path=str(manifest_path),
            error=str(exc),
        ) from exc
    actual_sha256 = hashlib.sha256(raw).hexdigest().upper()
    if strict and (len(raw) != CUDA_REDIST_MANIFEST_BYTES or actual_sha256 != CUDA_REDIST_MANIFEST_SHA256):
        raise _manifest_error(
            "ASR_TOOLCHAIN_CORRUPT",
            "CUDA redistributable manifest size or SHA-256 does not match the pinned source",
            path=str(manifest_path),
            expected_bytes=CUDA_REDIST_MANIFEST_BYTES,
            actual_bytes=len(raw),
            expected_sha256=CUDA_REDIST_MANIFEST_SHA256,
            actual_sha256=actual_sha256,
        )
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _manifest_error(
            "ASR_TOOLCHAIN_INVALID", "CUDA redistributable manifest is not valid UTF-8 JSON", path=str(manifest_path)
        ) from exc
    if not isinstance(manifest, dict):
        raise _manifest_error("ASR_TOOLCHAIN_INVALID", "CUDA redistributable manifest must be a JSON object")
    if manifest.get("release_label") != CUDA_REDIST_LABEL or manifest.get("release_product") != CUDA_REDIST_PRODUCT:
        raise _manifest_error(
            "ASR_TOOLCHAIN_INVALID",
            "CUDA redistributable manifest release does not match the pinned 13.3 CUDA release",
            expected_label=CUDA_REDIST_LABEL,
            actual_label=manifest.get("release_label"),
            expected_product=CUDA_REDIST_PRODUCT,
            actual_product=manifest.get("release_product"),
        )
    return manifest


def _safe_relative_parts(value: str, *, label: str) -> tuple[str, ...]:
    raw = str(value).replace("\\", "/")
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw)
    if not raw or raw.startswith("/") or raw.startswith("//") or windows.drive or windows.root:
        raise _manifest_error("ASR_TOOLCHAIN_INVALID", f"{label} must be a relative path", path=raw)
    parts = tuple(part for part in posix.parts if part not in {""})
    if not parts or any(part in {".", ".."} for part in parts):
        raise _manifest_error("ASR_TOOLCHAIN_INVALID", f"{label} contains an unsafe path", path=raw)
    return parts


def select_cuda_packages(
    manifest: Mapping[str, Any],
    components: Sequence[str] = CUDA_WINDOWS_BUILD_COMPONENTS,
) -> tuple[CudaPackage, ...]:
    """Select and validate the pinned Windows package descriptors."""

    selected: list[CudaPackage] = []
    for component in components:
        package = manifest.get(component)
        if not isinstance(package, Mapping):
            raise _manifest_error(
                "ASR_TOOLCHAIN_MISSING", "CUDA manifest package is missing", package=component
            )
        platform_entry = package.get(CUDA_REDIST_PLATFORM)
        if not isinstance(platform_entry, Mapping):
            raise _manifest_error(
                "ASR_TOOLCHAIN_MISSING",
                "CUDA manifest has no Windows x86_64 archive for package",
                package=component,
                platform=CUDA_REDIST_PLATFORM,
            )
        relative_path = platform_entry.get("relative_path")
        sha256 = str(platform_entry.get("sha256", "")).lower()
        size = platform_entry.get("size")
        if not isinstance(relative_path, str) or not isinstance(size, (int, str)) or isinstance(size, bool):
            raise _manifest_error(
                "ASR_TOOLCHAIN_INVALID", "CUDA manifest package metadata is incomplete", package=component
            )
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise _manifest_error(
                "ASR_TOOLCHAIN_INVALID", "CUDA manifest package SHA-256 is invalid", package=component
            )
        try:
            size_int = int(size)
        except (TypeError, ValueError) as exc:
            raise _manifest_error(
                "ASR_TOOLCHAIN_INVALID", "CUDA manifest package size is invalid", package=component
            ) from exc
        if size_int < 0:
            raise _manifest_error("ASR_TOOLCHAIN_INVALID", "CUDA manifest package size is negative", package=component)
        _safe_relative_parts(relative_path, label=f"{component}.relative_path")
        selected.append(
            CudaPackage(
                package=component,
                name=str(package.get("name", component)),
                version=str(package.get("version", "")),
                license=str(package.get("license", "")),
                license_path=str(package.get("license_path", "")),
                relative_path=relative_path,
                sha256=sha256,
                size=size_int,
            )
        )
    return tuple(selected)


def package_archive_path(transfer_root: Path | str, package: CudaPackage) -> Path:
    """Resolve one archive beneath an external transfer root.

    The canonical layout mirrors ``relative_path``.  A flat transfer directory
    containing the manifest's archive basename is also accepted because the
    transport handoff uses that layout; the exact nested path wins when both
    are present.
    """

    root = _external_root(transfer_root, "CUDA transfer root").resolve()
    relative_parts = _safe_relative_parts(package.relative_path, label="package archive path")
    candidate = root.joinpath(*relative_parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise _manifest_error(
            "ASR_TOOLCHAIN_INVALID", "package archive escapes the transfer root", path=str(candidate)
        ) from exc
    if candidate.is_file():
        return candidate
    flat_candidate = root / relative_parts[-1]
    if flat_candidate.is_file():
        return flat_candidate
    return candidate


def _safe_zip_member_parts(name: str) -> tuple[str, ...]:
    return _safe_relative_parts(name, label="ZIP member")


def _archive_diagnostic(package: CudaPackage, archive: Path) -> dict[str, Any]:
    base = {
        "package": package.package,
        "path": str(archive),
        "expected_size": package.size,
        "expected_sha256": package.sha256,
    }
    if not archive.is_file():
        return {**base, "status": "missing", "code": "ASR_TOOLCHAIN_MISSING", "reason": "archive is missing"}
    actual_size = archive.stat().st_size
    if actual_size != package.size:
        return {
            **base,
            "status": "corrupt",
            "code": "ASR_TOOLCHAIN_CORRUPT",
            "reason": "archive size does not match manifest",
            "actual_size": actual_size,
        }
    actual_sha256 = _sha256(archive)
    if actual_sha256.lower() != package.sha256.lower():
        return {
            **base,
            "status": "corrupt",
            "code": "ASR_TOOLCHAIN_CORRUPT",
            "reason": "archive SHA-256 does not match manifest",
            "actual_sha256": actual_sha256,
        }
    try:
        with zipfile.ZipFile(archive) as handle:
            members = tuple(handle.infolist())
            for member in members:
                _safe_zip_member_parts(member.filename)
                mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise _manifest_error("ASR_TOOLCHAIN_INVALID", "ZIP symlink members are not allowed")
            bad_member = handle.testzip()
            if bad_member is not None:
                return {
                    **base,
                    "status": "corrupt",
                    "code": "ASR_TOOLCHAIN_CORRUPT",
                    "reason": "ZIP CRC check failed",
                    "member": bad_member,
                }
    except AsrError as exc:
        return {**base, "status": "invalid", "code": exc.code, "reason": exc.reason, "details": exc.details}
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return {
            **base,
            "status": "corrupt",
            "code": "ASR_TOOLCHAIN_CORRUPT",
            "reason": "archive is not a readable ZIP",
            "error": str(exc),
        }
    return {**base, "status": "ok", "actual_size": actual_size, "actual_sha256": actual_sha256}


def inspect_cuda_transfer(
    manifest_path: Path | str,
    transfer_root: Path | str,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Verify every selected archive without writing or downloading."""

    manifest = load_manifest(manifest_path, strict=strict)
    packages = select_cuda_packages(manifest)
    root = _external_root(transfer_root, "CUDA transfer root")
    diagnostics = [_archive_diagnostic(package, package_archive_path(root, package)) for package in packages]
    statuses = {item["status"] for item in diagnostics}
    status = "ready"
    if "invalid" in statuses:
        status = "invalid"
    elif "corrupt" in statuses:
        status = "corrupt"
    elif "missing" in statuses:
        status = "missing"
    raw = Path(manifest_path).read_bytes()
    return {
        "status": status,
        "manifest": {
            "path": str(Path(manifest_path)),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest().upper(),
            "release_label": manifest["release_label"],
            "release_product": manifest["release_product"],
        },
        "transfer_root": str(root),
        "platform": CUDA_REDIST_PLATFORM,
        "packages": diagnostics,
        "native_acceptance": False,
    }


def build_command(
    source_root: Path | str,
    build_dir: Path | str,
    *,
    cuda_arch: str = "86",
    script_path: Path | str | None = None,
) -> list[str]:
    """Return the fixed source's truthful CUDA/HTTP ASR build command."""

    source = Path(source_root)
    build = Path(build_dir)
    script = Path(script_path) if script_path is not None else source / "scripts" / "windows" / "build.ps1"
    return [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Backend",
        "cuda",
        "-AsrOnly",
        "-Http",
        "-CudaArch",
        str(cuda_arch),
        "-Architecture",
        "x64",
        "-Compiler",
        "msvc",
        "-BuildDir",
        str(build),
    ]


def plan_cuda_toolchain(
    manifest_path: Path | str,
    transfer_root: Path | str,
    toolchain_root: Path | str,
    *,
    source_root: Path | str | None = None,
    build_dir: Path | str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Create a no-write assembly/build plan with precise transfer URLs."""

    root = _external_root(toolchain_root, "CUDA toolchain root")
    transfer = _external_root(transfer_root, "CUDA transfer root")
    inspection = inspect_cuda_transfer(manifest_path, transfer, strict=strict)
    packages = select_cuda_packages(load_manifest(manifest_path, strict=strict))
    package_plan = []
    for package, diagnostic in zip(packages, inspection["packages"], strict=True):
        package_plan.append(
            {
                **package.to_dict(),
                "archive": diagnostic["path"],
                "download_url": urljoin(CUDA_REDIST_BASE_URL, package.relative_path),
                "verification": diagnostic,
            }
        )
    result: dict[str, Any] = {
        "mode": "dry-run",
        "idempotent": True,
        "status": inspection["status"],
        "native_acceptance": False,
        "manifest_url": CUDA_REDIST_MANIFEST_URL,
        "manifest": inspection["manifest"],
        "transfer_root": str(transfer),
        "toolchain_root": str(root),
        "platform": CUDA_REDIST_PLATFORM,
        "packages": package_plan,
        "static_source_closure": True,
    }
    if source_root is not None:
        build_path = Path(build_dir) if build_dir is not None else Path(source_root) / "build-cuda-asr-http"
        result["build"] = {
            "source_root": str(Path(source_root)),
            "build_dir": str(build_path),
            "command": build_command(source_root, build_path),
            "cuda_architecture": "86",
            "http": True,
            "asr_only": True,
        }
    return result


def _read_marker(root: Path) -> dict[str, Any] | None:
    marker = root / CUDA_TOOLCHAIN_MARKER
    if not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _marker_matches_root(marker: Mapping[str, Any], root: Path) -> bool:
    return (
        marker.get("owner") == CUDA_TOOLCHAIN_OWNER
        and str(marker.get("toolchain_root", "")) == str(root.resolve())
    )


def _expected_toolchain_files(root: Path) -> tuple[Path, ...]:
    return (
        root / "bin" / "nvcc.exe",
        root / "bin" / "ptxas.exe",
        root / "bin" / "ctadvisor.exe",
        root / "include" / "cuda_runtime.h",
        root / "include" / "cublas_v2.h",
        root / "lib" / "x64" / "cudart.lib",
        root / "lib" / "x64" / "cublas.lib",
        root / "lib" / "x64" / "cublasLt.lib",
        root / "nvvm" / "bin" / "cicc.exe",
    )


def cuda_toolchain_status(toolchain_root: Path | str) -> dict[str, Any]:
    """Return ownership/layout diagnostics; never claims native ASR readiness."""

    root = _external_root(toolchain_root, "CUDA toolchain root")
    base: dict[str, Any] = {
        "toolchain_root": str(root),
        "native_acceptance": False,
        "availability": "UNAVAILABLE",
    }
    if not root.exists():
        return {**base, "status": "missing", "code": "ASR_TOOLCHAIN_MISSING", "owned": False}
    if not root.is_dir():
        return {**base, "status": "corrupt", "code": "ASR_TOOLCHAIN_CORRUPT", "owned": False}
    marker = _read_marker(root)
    if marker is None:
        return {
            **base,
            "status": "unmanaged",
            "code": "ASR_TOOLCHAIN_INVALID",
            "owned": False,
            "reason": "toolchain root has no valid B05 ownership marker",
        }
    if not _marker_matches_root(marker, root):
        return {
            **base,
            "status": "invalid",
            "code": "ASR_TOOLCHAIN_INVALID",
            "owned": False,
            "reason": "toolchain ownership marker does not match this root",
        }
    missing = [str(path) for path in _expected_toolchain_files(root) if not path.is_file()]
    if missing:
        return {
            **base,
            "status": "corrupt",
            "code": "ASR_TOOLCHAIN_CORRUPT",
            "owned": True,
            "missing": missing,
            "marker": marker,
        }
    return {
        **base,
        "status": "ready",
        "code": None,
        "owned": True,
        "ready_for_build": True,
        "marker": marker,
        "missing": [],
    }


def _extract_archive(archive: Path, package: CudaPackage, staging: Path) -> None:
    with zipfile.ZipFile(archive) as handle:
        infos = tuple(handle.infolist())
        top_levels = {
            _safe_zip_member_parts(info.filename)[0]
            for info in infos
            if _safe_zip_member_parts(info.filename)
        }
        strip_root = next(iter(top_levels)) if len(top_levels) == 1 else None
        for info in infos:
            parts = _safe_zip_member_parts(info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise _manifest_error("ASR_TOOLCHAIN_INVALID", "ZIP symlink members are not allowed", package=package.package)
            relative = parts[1:] if strip_root is not None and parts[0] == strip_root else parts
            if not relative:
                continue
            if len(relative) == 1 and relative[0].lower().startswith("license"):
                destination = staging / "licenses" / package.package / relative[0]
            else:
                destination = staging.joinpath(*relative)
            try:
                destination.resolve(strict=False).relative_to(staging.resolve())
            except ValueError as exc:
                raise _manifest_error(
                    "ASR_TOOLCHAIN_INVALID", "ZIP member escapes the toolchain root", member=info.filename
                ) from exc
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise _manifest_error(
                    "ASR_TOOLCHAIN_INVALID",
                    "CUDA archives contain a colliding file",
                    package=package.package,
                    path=str(destination),
                )
            with handle.open(info, "r") as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)


def _marker_payload(manifest_info: Mapping[str, Any], packages: Sequence[CudaPackage], root: Path) -> dict[str, Any]:
    return {
        "owner": CUDA_TOOLCHAIN_OWNER,
        "schema_version": 1,
        "toolchain_root": str(root.resolve()),
        "manifest": dict(manifest_info),
        "packages": [package.to_dict() for package in packages],
        "layout": "flattened-single-root-with-package-licenses",
        "native_acceptance": False,
    }


def assemble_cuda_toolchain(
    manifest_path: Path | str,
    transfer_root: Path | str,
    toolchain_root: Path | str,
    *,
    apply: bool = False,
    strict: bool = True,
) -> dict[str, Any]:
    """Verify and optionally assemble a portable, owned CUDA prefix."""

    plan = plan_cuda_toolchain(manifest_path, transfer_root, toolchain_root, strict=strict)
    if not apply:
        return plan
    if plan["status"] != "ready":
        code = {
            "missing": "ASR_TOOLCHAIN_MISSING",
            "corrupt": "ASR_TOOLCHAIN_CORRUPT",
            "invalid": "ASR_TOOLCHAIN_INVALID",
        }.get(plan["status"], "ASR_TOOLCHAIN_INVALID")
        raise AsrError(code, "CUDA transfer is not ready for assembly", {"status": plan["status"], "plan": plan})

    root = _external_root(toolchain_root, "CUDA toolchain root")
    manifest = load_manifest(manifest_path, strict=strict)
    packages = select_cuda_packages(manifest)
    marker_info = _marker_payload(plan["manifest"], packages, root)
    if root.exists():
        status = cuda_toolchain_status(root)
        if status.get("status") == "ready" and status.get("marker", {}).get("manifest", {}).get("sha256") == plan["manifest"]["sha256"]:
            return {**plan, "mode": "applied", "idempotent": True, "assembled": False, "status": status}
        raise AsrError(
            "ASR_TOOLCHAIN_INVALID",
            "refusing to overwrite an existing non-identical CUDA toolchain root",
            {"root": str(root), "status": status},
        )

    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".b05-cuda-", dir=str(root.parent)))
    try:
        for package in packages:
            _extract_archive(package_archive_path(transfer_root, package), package, staging)
        marker = staging / CUDA_TOOLCHAIN_MARKER
        marker.write_text(json.dumps(marker_info, indent=2, sort_keys=True), encoding="utf-8")
        staging.replace(root)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    status = cuda_toolchain_status(root)
    if status.get("status") != "ready":
        raise AsrError("ASR_TOOLCHAIN_CORRUPT", "assembled CUDA toolchain failed layout validation", {"status": status})
    return {**plan, "mode": "applied", "idempotent": False, "assembled": True, "status": status}


def uninstall_cuda_toolchain(toolchain_root: Path | str, *, apply: bool = False) -> dict[str, Any]:
    """Remove only an exact B05-owned CUDA prefix; absent roots are idempotent."""

    root = _external_root(toolchain_root, "CUDA toolchain root")
    status = cuda_toolchain_status(root)
    plan = {
        "mode": "dry-run",
        "toolchain_root": str(root),
        "owned": bool(status.get("owned")),
        "status": status,
        "deleted": False,
    }
    if not apply:
        return plan
    if not root.exists():
        return {**plan, "mode": "applied", "idempotent": True}
    marker = _read_marker(root)
    if marker is None or not _marker_matches_root(marker, root):
        raise AsrError(
            "ASR_TOOLCHAIN_INVALID",
            "refusing to delete a CUDA root without the exact B05 ownership marker",
            {"root": str(root)},
        )
    shutil.rmtree(root)
    return {**plan, "mode": "applied", "idempotent": False, "deleted": True}


def build_environment(
    toolchain_root: Path | str,
    *,
    cmake_path: Path | str | None = None,
    ninja_path: Path | str | None = None,
    vswhere_path: Path | str | None = None,
    cuda_arch: str = "86",
) -> dict[str, Any]:
    """Describe the VS/CMake/Ninja/CUDA environment without mutating the host."""

    root = _external_root(toolchain_root, "CUDA toolchain root")
    status = cuda_toolchain_status(root)
    diagnostics: list[dict[str, Any]] = []

    def resolve_tool(value: Path | str | None, command: str) -> Path | None:
        if value is not None:
            candidate = Path(value)
            return candidate if candidate.exists() else None
        found = shutil.which(command)
        return Path(found) if found else None

    cmake = resolve_tool(cmake_path, "cmake")
    ninja = resolve_tool(ninja_path, "ninja")
    vswhere = resolve_tool(vswhere_path, "vswhere")
    for label, value in (("cmake", cmake), ("ninja", ninja), ("vswhere", vswhere)):
        if value is None:
            diagnostics.append({"code": "ASR_DEPENDENCY_MISSING", "tool": label})

    vs_installation = None
    if vswhere is not None:
        try:
            result = subprocess.run(
                [
                    str(vswhere),
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property",
                    "installationPath",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            vs_installation = result.stdout.strip() or None
            if result.returncode != 0 or not vs_installation:
                diagnostics.append({"code": "ASR_DEPENDENCY_MISSING", "tool": "vswhere-vs2022-msvc", "stderr": result.stderr.strip()})
        except OSError as exc:
            diagnostics.append({"code": "ASR_DEPENDENCY_MISSING", "tool": "vswhere", "error": str(exc)})

    path_entries = [str(root / "bin")]
    if cmake is not None:
        path_entries.append(str(cmake.parent))
    if ninja is not None:
        path_entries.append(str(ninja.parent))
    existing_path = os.environ.get("PATH", "")
    if existing_path:
        path_entries.append(existing_path)
    env = {
        "CUDA_PATH": str(root),
        "CUDA_HOME": str(root),
        "CUDACXX": str(root / "bin" / "nvcc.exe"),
        "CMAKE_CUDA_ARCHITECTURES": str(cuda_arch),
        "CMAKE_CUDA_HOST_COMPILER": "cl",
        "PATH": os.pathsep.join(path_entries),
    }
    return {
        "status": "ready" if status.get("status") == "ready" and not diagnostics else "unavailable",
        "cuda": status,
        "host_compiler": "cl",
        "vs_installation": vs_installation,
        "cmake": str(cmake) if cmake else None,
        "ninja": str(ninja) if ninja else None,
        "vswhere": str(vswhere) if vswhere else None,
        "cuda_architecture": str(cuda_arch),
        "environment": env,
        "diagnostics": diagnostics,
        "native_acceptance": False,
    }
