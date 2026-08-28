"""Build the single-file Windows setup wrapper from verified offline inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath


MANIFEST_NAME = "offline-core-assets.json"
SETUP_NAME = "Olivia-Setup-x64.exe"
BUILD_CONTROL_FILES = {
    "installer/build_windows_setup.py",
    "installer/setup-build-requirements.txt",
    "installer/windows_setup.iss",
}
RELEASE_PREFIXES = (
    "asr/",
    "control_center/",
    "contracts/",
    "linli_character/",
    "live/",
    "media_state/",
    "runtime/",
    "tts/",
    "visual_driver/",
)
RELEASE_ROOT_FILES = {
    "LICENSE",
    "NOTICE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "companion_memory_context.py",
    "conversation_memory_admin.py",
    "conversation_memory_delivery.py",
    "conversation_memory_outbox.py",
    "conversation_memory_port.py",
    "conversation_memory_runtime.py",
    "http_contract.py",
    "latentsync_reply.py",
    "letter_status.py",
    "letter_triage.py",
    "llm_config.example.json",
    "llm_gateway.py",
    "local_memory.py",
    "local_server.py",
    "mem0_capability_install.py",
    "mem0_embedding_install.py",
    "mem0_memory.py",
    "memory.py",
    "memory_isolation_case01.py",
    "memory_port.py",
    "memory_prompt.py",
    "music_reply.py",
    "original_client_companion_api.py",
    "original_client_companion_backend.py",
    "original_client_companion_mutation_api.py",
    "original_client_companion_mutation_backend.py",
    "original_client_capability_api.py",
    "original_client_letter_contract.py",
    "original_client_media_http.py",
    "original_client_server.py",
    "original_client_video_capability_api.py",
    "original_client_setup_api.py",
    "original_client_settings_ui.py",
    "original_client_update_api.py",
    "patch_companion_settings.py",
    "patch_feapp.py",
    "patch_webplayer.py",
    "persona_assembly.py",
    "persona_loader.py",
    "persona_provider.py",
    "private_world_admin.py",
    "private_world_candidate.py",
    "private_world_candidates.py",
    "private_world_commands.py",
    "private_world_ledger.py",
    "private_world_port.py",
    "private_world_reducer.py",
    "private_world_service.py",
    "reply_context.py",
    "reply_delivery.py",
    "reply_media.py",
    "reply_model_quality.py",
    "reply_orchestrator.py",
    "reply_pipeline.py",
    "reply_reviewer.py",
    "song_content.py",
    "version.json",
    "video_reply_settings.py",
    "voice_direction.py",
    "video_capability_install.py",
}
RELEASE_INSTALLER_FILES = {
    "installer/__init__.py",
    "installer/__main__.py",
    "installer/bootstrap_install.py",
    "installer/component_update.py",
    "installer/configure.py",
    "installer/Create-Shortcut.ps1",
    "installer/assets/olivia.ico",
    "installer/full_patch.py",
    "installer/full-patch-manifest.json",
    "installer/Install.ps1",
    "installer/mem0-capability-manifest.json",
    "installer/mem0-runtime-artifacts.json",
    "installer/mem0-runtime-requirements.txt",
    "installer/provision_mem0_embedding.py",
    "installer/runtime-requirements.txt",
    "installer/start_local.py",
    "installer/uninstall.py",
    "installer/uninstall_safety.py",
    "installer/verify_mem0_runtime.py",
    "installer/version_launcher.py",
    "installer/video-capability-manifest.json",
    "installer/seed-vc-overlap-frames.patch",
}
RELEASE_TOOL_FILES = {
    "tools/asr_healthcheck.py",
    "tools/asr_manage.py",
    "tools/asset_manifest.py",
    "tools/b05_native_http_snapshot.patch",
    "tools/B10A_cli.py",
    "tools/B10B_cli.py",
    "tools/download_third_party.py",
    "tools/healthcheck.py",
    "tools/Install-ThirdParty.ps1",
    "tools/live_b10b.py",
    "tools/live_healthcheck.py",
    "tools/livetalking_runtime.py",
    "tools/livetalking_worker.py",
    "tools/memory_import.py",
    "tools/minimax_music3_worker.py",
    "tools/minimax_profile.py",
    "tools/music_renderer.py",
    "tools/tts_cli.py",
}
REQUIRED_PAYLOAD_FILES = {
    "installer/Install.ps1",
    "installer/runtime-requirements.txt",
}


class SetupBuildError(RuntimeError):
    """Stable setup-build failure code."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _git_tracked_files(source: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(source), "ls-files", "-z"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupBuildError("SETUP_GIT_LIST_FAILED") from exc
    return {
        item.decode("utf-8").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    }


def _git_dirty_files(source: Path) -> set[str]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                os.fspath(source),
                "diff",
                "--name-only",
                "-z",
                "HEAD",
                "--",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupBuildError("SETUP_GIT_DIFF_FAILED") from exc
    return {
        item.decode("utf-8").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    }


def _is_release_file(relative: str) -> bool:
    return (
        relative in RELEASE_ROOT_FILES
        or relative in RELEASE_INSTALLER_FILES
        or relative in RELEASE_TOOL_FILES
        or relative.startswith(RELEASE_PREFIXES)
    )


def _safe_file(root: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or logical.is_absolute()
        or any(part in {"", ".", ".."} for part in logical.parts)
    ):
        raise SetupBuildError("SETUP_OFFLINE_ASSET_PATH_INVALID")
    candidate = root.joinpath(*logical.parts)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SetupBuildError("SETUP_OFFLINE_ASSET_PATH_INVALID") from exc
    current = root
    for part in logical.parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise SetupBuildError("SETUP_PATH_REPARSE_POINT")
    if not candidate.is_file():
        raise SetupBuildError("SETUP_OFFLINE_ASSET_MISSING")
    return candidate


def _load_and_verify_manifest(
    source: Path,
    offline: Path,
    *,
    validate_schema: bool,
) -> dict[str, object]:
    manifest_path = offline / MANIFEST_NAME
    if not manifest_path.is_file() or _is_reparse_point(manifest_path):
        raise SetupBuildError("SETUP_OFFLINE_MANIFEST_MISSING")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupBuildError("SETUP_OFFLINE_MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict):
        raise SetupBuildError("SETUP_OFFLINE_MANIFEST_INVALID")

    if validate_schema:
        try:
            import jsonschema
        except ImportError as exc:
            raise SetupBuildError("SETUP_SCHEMA_VALIDATOR_MISSING") from exc
        try:
            schema = json.loads(
                (source / "contracts" / "offline_core_assets.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            jsonschema.validate(manifest, schema)
        except (OSError, UnicodeError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
            raise SetupBuildError("SETUP_OFFLINE_MANIFEST_INVALID") from exc

    requirements = source / "installer" / "runtime-requirements.txt"
    if not requirements.is_file() or _is_reparse_point(requirements):
        raise SetupBuildError("SETUP_REQUIREMENTS_MISSING")
    if manifest.get("requirements_sha256") != _sha256(requirements):
        raise SetupBuildError("SETUP_REQUIREMENTS_HASH_MISMATCH")

    entries: list[object] = [manifest.get("python_runtime"), manifest.get("pip_bootstrap")]
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list):
        raise SetupBuildError("SETUP_OFFLINE_MANIFEST_INVALID")
    entries.extend(wheels)
    declared_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SetupBuildError("SETUP_OFFLINE_MANIFEST_INVALID")
        relative = entry.get("path")
        size = entry.get("size_bytes")
        digest = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or type(size) is not int
            or size < 1
            or not isinstance(digest, str)
            or len(digest) != 64
            or relative in declared_paths
        ):
            raise SetupBuildError("SETUP_OFFLINE_MANIFEST_INVALID")
        asset = _safe_file(offline, relative)
        if asset.stat().st_size != size or _sha256(asset) != digest:
            raise SetupBuildError("SETUP_OFFLINE_ASSET_HASH_MISMATCH")
        declared_paths.add(relative)

    actual_paths = {
        path.relative_to(offline).as_posix()
        for path in offline.rglob("*")
        if path.is_file()
    }
    if any(_is_reparse_point(path) for path in offline.rglob("*")):
        raise SetupBuildError("SETUP_PATH_REPARSE_POINT")
    if actual_paths != declared_paths | {MANIFEST_NAME}:
        raise SetupBuildError("SETUP_OFFLINE_ASSET_SET_MISMATCH")
    return manifest


def prepare_setup_payload(
    source: Path,
    offline: Path,
    destination: Path,
    *,
    validate_schema: bool = True,
) -> None:
    source = source.expanduser().resolve()
    offline = offline.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise SetupBuildError("SETUP_PAYLOAD_EXISTS")
    _load_and_verify_manifest(source, offline, validate_schema=validate_schema)
    tracked = _git_tracked_files(source)
    if not REQUIRED_PAYLOAD_FILES.issubset(tracked):
        raise SetupBuildError("SETUP_REQUIRED_PAYLOAD_MISSING")
    selected = sorted(relative for relative in tracked if _is_release_file(relative))
    build_inputs = set(selected) | BUILD_CONTROL_FILES
    if not build_inputs.issubset(tracked):
        raise SetupBuildError("SETUP_REQUIRED_PAYLOAD_MISSING")
    if build_inputs & _git_dirty_files(source):
        raise SetupBuildError("SETUP_SOURCE_DIRTY")
    staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True)
        for relative in selected:
            source_path = _safe_file(source, relative)
            target = staging.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
        shutil.copytree(offline, staging / "offline")
        os.replace(staging, destination)
    except SetupBuildError:
        raise
    except OSError as exc:
        raise SetupBuildError("SETUP_PAYLOAD_BUILD_FAILED") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _find_iscc(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    located = shutil.which("ISCC.exe")
    if located:
        candidates.append(Path(located))
    for environment_name in ("LOCALAPPDATA", "ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(environment_name)
        if not base:
            continue
        for version in ("7", "6"):
            candidates.append(Path(base) / f"Inno Setup {version}" / "ISCC.exe")
            candidates.append(Path(base) / "Programs" / f"Inno Setup {version}" / "ISCC.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise SetupBuildError("SETUP_ISCC_NOT_FOUND")


def build_windows_setup(
    source: Path,
    offline: Path,
    output: Path,
    *,
    version: str,
    iscc: Path | None = None,
) -> dict[str, object]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    setup = output / SETUP_NAME
    checksum = output / f"{SETUP_NAME}.sha256"
    if setup.exists() or checksum.exists():
        raise SetupBuildError("SETUP_OUTPUT_EXISTS")
    payload = output / f".setup-payload-{uuid.uuid4().hex}"
    compiler = _find_iscc(iscc)
    try:
        prepare_setup_payload(source, offline, payload)
        command = [
            os.fspath(compiler),
            f"/DPayloadRoot={payload}",
            f"/DOutputDir={output}",
            f"/DAppVersion={version}",
            os.fspath(source / "installer" / "windows_setup.iss"),
        ]
        result = subprocess.run(command, check=False, timeout=900)
        if result.returncode != 0 or not setup.is_file():
            raise SetupBuildError("SETUP_COMPILE_FAILED")
        digest = _sha256(setup)
        checksum.write_text(f"{digest}  {SETUP_NAME}\n", encoding="ascii")
        return {
            "status": "OK",
            "setup": os.fspath(setup),
            "size_bytes": setup.stat().st_size,
            "sha256": digest,
        }
    except SetupBuildError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupBuildError("SETUP_BUILD_FAILED") from exc
    finally:
        shutil.rmtree(payload, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build-windows-setup")
    parser.add_argument("--source", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--offline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--iscc", type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_windows_setup(
            args.source,
            args.offline,
            args.output,
            version=args.version,
            iscc=args.iscc,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except SetupBuildError as exc:
        print(json.dumps({"status": "ERROR", "code": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
