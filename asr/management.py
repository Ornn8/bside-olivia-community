"""Idempotent, explicit install/switch/uninstall operations for B05."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .config import (
    MODEL_FILENAME,
    MODEL_LICENSE,
    MODEL_REPO,
    MODEL_REVISION,
    MODEL_SHA256,
    RUNTIME_LICENSE,
    RUNTIME_REPO,
    RUNTIME_REVISION,
    AsrConfig,
    is_local_absolute_path,
)
from .errors import AsrError


GGML_REVISION = "c03b4e2bcece5134827881af90242086daf75be5"
CPP_HTTPLIB_REVISION = "62d899feac3cf9215a55f2b43da250fdd98d2156"
TRANSFER_SOURCE_DIR = "NeMo-Speech.cpp"
HTTP_SNAPSHOT_PATCH = "tools/b05_native_http_snapshot.patch"


def _external_paths(config: AsrConfig) -> bool:
    paths = [config.runtime_root, config.model_root, config.cache_root]
    return all(is_local_absolute_path(path) for path in paths)


def _external_path(path: Path | str, *, label: str) -> Path:
    path = Path(path)
    if not is_local_absolute_path(path):
        raise AsrError("ASR_CONFIG_INVALID", f"{label} must be an absolute local Windows path", {"path": str(path)})
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_output(source_root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={source_root}",
                "-C",
                str(source_root),
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AsrError(
            "ASR_RUNTIME_MISSING",
            "transfer source is not a usable git checkout",
            {"source_root": str(source_root), "diagnostic": type(exc).__name__},
        ) from exc
    return completed.stdout.strip()


def _validate_transfer_source(transfer_root: Path) -> dict[str, Any]:
    transfer_root = _external_path(transfer_root, label="transfer_root")
    source_root = transfer_root / TRANSFER_SOURCE_DIR
    if not source_root.is_dir():
        raise AsrError("ASR_RUNTIME_MISSING", "fixed NeMo-Speech.cpp source is missing", {"path": str(source_root)})
    runtime_revision = _git_output(source_root, "rev-parse", "HEAD")
    if runtime_revision != RUNTIME_REVISION:
        raise AsrError(
            "ASR_CONFIG_INVALID",
            "transfer source revision does not match the pinned runtime",
            {"expected": RUNTIME_REVISION, "actual": runtime_revision},
        )
    gitlinks = _git_output(source_root, "ls-tree", "HEAD", "ggml", "third_party/cpp-httplib")
    revisions: dict[str, str] = {}
    for line in gitlinks.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            revisions[fields[3]] = fields[2]
    expected = {"ggml": GGML_REVISION, "third_party/cpp-httplib": CPP_HTTPLIB_REVISION}
    if any(revisions.get(path) != revision for path, revision in expected.items()):
        raise AsrError(
            "ASR_CONFIG_INVALID",
            "transfer source submodule pins do not match the fixed upstream closure",
            {"expected": expected, "actual": revisions},
        )
    for relative in ("ggml/include/ggml.h", "third_party/cpp-httplib/httplib.h"):
        if not (source_root / relative).is_file():
            raise AsrError(
                "ASR_RUNTIME_MISSING",
                "fixed runtime checkout is missing a required submodule file",
                {"path": str(source_root / relative)},
            )
    return {
        "root": str(source_root),
        "revision": runtime_revision,
        "submodules": expected,
        "license": RUNTIME_LICENSE,
    }


def _validate_model(path: Path) -> dict[str, Any]:
    path = _external_path(path, label="model_path")
    if not path.is_file():
        raise AsrError("ASR_MODEL_MISSING", "fixed model file is missing", {"path": str(path)})
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != MODEL_SHA256:
        raise AsrError(
            "ASR_MODEL_CORRUPT",
            "fixed model SHA-256 does not match the pinned model",
            {"expected": MODEL_SHA256, "actual": actual_sha256},
        )
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": actual_sha256}


def _patch_provenance() -> dict[str, Any]:
    patch_path = Path(__file__).resolve().parents[1] / HTTP_SNAPSHOT_PATCH
    return {
        "path": str(patch_path),
        "sha256": _sha256_file(patch_path) if patch_path.is_file() else None,
        "purpose": "fixed EngineRegistry snapshot for /v1/models lock ordering",
    }


def install_plan(config: AsrConfig, *, transfer_root: Path | None = None) -> dict[str, Any]:
    if config.strict_storage:
        config.validate_storage_roots()
    if transfer_root is not None:
        transfer_root = _external_path(transfer_root, label="transfer_root")
    return {
        "mode": "dry-run",
        "idempotent": True,
        "paths_are_external": _external_paths(config),
        "runtime": {
            "repo": RUNTIME_REPO,
            "revision": RUNTIME_REVISION,
            "license": RUNTIME_LICENSE,
            "release": "none",
            "build": "scripts/windows/build.ps1 -Backend cuda -Http",
            "source": str(transfer_root / TRANSFER_SOURCE_DIR) if transfer_root else None,
            "submodules": {"ggml": GGML_REVISION, "third_party/cpp-httplib": CPP_HTTPLIB_REVISION},
        },
        "model": {
            "repo": MODEL_REPO,
            "revision": MODEL_REVISION,
            "filename": MODEL_FILENAME,
            "sha256": MODEL_SHA256,
            "license": MODEL_LICENSE,
            "download": "disabled; use an explicit verified transfer_root asset",
        },
        "paths": {
            "runtime_root": str(config.runtime_root),
            "model_root": str(config.model_root),
            "cache_root": str(config.cache_root),
            "acceptance_manifest": str(config.acceptance_manifest),
        },
        "provenance": {
            "policy": "assembly-only",
            "http_snapshot_patch": _patch_provenance(),
            "replacement_boundary": "replace only the external runtime executable/source reference; B05 owns no inference engine",
            "uninstall_boundary": "remove only the B05 ownership manifest and metadata roots; preserve external runtime, model, cache, and evidence",
        },
    }


def install(config: AsrConfig, *, apply: bool = False, transfer_root: Path | None = None) -> dict[str, Any]:
    plan = install_plan(config, transfer_root=transfer_root)
    if not apply:
        return plan
    if not plan["paths_are_external"]:
        raise AsrError("ASR_CONFIG_INVALID", "install roots must be absolute local Windows paths")
    if transfer_root is None:
        raise AsrError(
            "ASR_CONFIG_INVALID",
            "apply requires an explicit local absolute transfer_root; network downloads are disabled",
        )
    source = _validate_transfer_source(transfer_root)
    model = _validate_model(config.effective_model_path)
    runtime_executable = _external_path(config.effective_runtime_executable, label="runtime_executable")
    if not runtime_executable.is_file():
        raise AsrError("ASR_RUNTIME_MISSING", "native runtime executable is missing", {"path": str(runtime_executable)})
    config.runtime_root.mkdir(parents=True, exist_ok=True)
    config.model_root.mkdir(parents=True, exist_ok=True)
    config.cache_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "owner": "b05-streaming-asr",
        "mode": "external-transfer-assembly",
        "runtime_repo": RUNTIME_REPO,
        "runtime_revision": RUNTIME_REVISION,
        "runtime_source": source,
        "runtime_executable": str(runtime_executable),
        "model_repo": MODEL_REPO,
        "model_revision": MODEL_REVISION,
        "model": {**model, "repo": MODEL_REPO, "revision": MODEL_REVISION, "license": MODEL_LICENSE},
        "model_license": MODEL_LICENSE,
        "runtime_license": RUNTIME_LICENSE,
        "http_snapshot_patch": _patch_provenance(),
        "acceptance_manifest": str(config.acceptance_manifest),
        "replacement_boundary": plan["provenance"]["replacement_boundary"],
        "uninstall_boundary": plan["provenance"]["uninstall_boundary"],
    }
    config.runtime_root.joinpath(".b05-install.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {**plan, "mode": "applied", "manifest": manifest, "network": {"called": False}}


def switch_provider(
    config_path: Path, provider: str, *, base_config: AsrConfig | None = None
) -> AsrConfig:
    if provider not in {"text-fallback", "nemotron-speech-cpp"}:
        raise AsrError("ASR_CONFIG_INVALID", f"unsupported provider: {provider}")
    config_path = Path(config_path)
    config = AsrConfig.from_json(config_path) if config_path.is_file() else (base_config or AsrConfig())
    config = AsrConfig.from_mapping({**config.to_dict(), "provider": provider, "strict_storage": config.strict_storage})
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")
    return config


def uninstall_plan(config: AsrConfig) -> dict[str, Any]:
    owned_manifest = config.runtime_root / ".b05-install.json"
    owned = owned_manifest.is_file()
    return {
        "mode": "dry-run",
        "owned_manifest": str(owned_manifest),
        "owned": owned,
        "paths": [str(config.runtime_root), str(config.model_root), str(config.cache_root)],
        "deleted": [],
        "preserved_external_paths": [str(config.effective_runtime_executable), str(config.effective_model_path)],
        "boundary": "only B05-owned roots are removable; external runtime/model/evidence are preserved",
    }


def uninstall(config: AsrConfig, *, apply: bool = False) -> dict[str, Any]:
    plan = uninstall_plan(config)
    if not apply:
        return plan
    if not plan["owned"]:
        raise AsrError("ASR_CONFIG_INVALID", "refusing to delete paths without the B05 ownership manifest")
    if not _external_paths(config):
        raise AsrError("ASR_CONFIG_INVALID", "uninstall roots must be absolute local Windows paths")
    manifest_path = Path(config.runtime_root) / ".b05-install.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AsrError("ASR_CONFIG_INVALID", "refusing to delete without a valid B05 ownership manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("owner") != "b05-streaming-asr":
        raise AsrError("ASR_CONFIG_INVALID", "refusing to delete without a valid B05 ownership manifest")

    # The manifest is the only file this tool creates in the managed roots.
    # Never recursively delete a configured directory: it may contain an
    # external runtime, model, cache, or user files.
    deleted = [str(manifest_path)]
    try:
        manifest_path.unlink()
    except OSError as exc:
        raise AsrError("ASR_CONFIG_INVALID", "B05 ownership manifest cannot be removed") from exc
    return {**plan, "mode": "applied", "deleted": deleted}
