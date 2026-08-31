"""Build the single-file Windows setup wrapper from verified offline inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
import wave
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from installer.component_update import ComponentUpdateError, _validate_relative_path


MANIFEST_NAME = "offline-core-assets.json"
SETUP_NAME = "Olivia-Setup-x64.exe"
VIDEO_RUNTIME_SIDECAR_NAME = "Olivia-video-runtime-private.zip"
VIDEO_OFFLINE_SIDECAR_NAME = "Olivia-video-offline-private"
PRIVATE_RECEIPT_NAME = "Olivia-Setup-x64.receipt.json"
VOICE_REFERENCE_PATH = "voice/olivia-reference.wav"
FORBIDDEN_MEDIA_SUFFIXES = {
    ".aac",
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
    ".wmv",
}
VIDEO_RUNTIME_PATH = VIDEO_RUNTIME_SIDECAR_NAME
VIDEO_RUNTIME_ENVIRONMENT_KEYS = {
    "OLIVIA_COSYVOICE_PYTHON",
    "OLIVIA_LATENTSYNC_PYTHON",
    "OLIVIA_MINIMAX_COMFY_PYTHON",
    "OLIVIA_ROFORMER_PYTHON",
}
VIDEO_RUNTIME_COMPONENT_ENVIRONMENT = {
    "cosyvoice": "OLIVIA_COSYVOICE_PYTHON",
    "latentsync": "OLIVIA_LATENTSYNC_PYTHON",
    "minimax": "OLIVIA_MINIMAX_COMFY_PYTHON",
    "roformer": "OLIVIA_ROFORMER_PYTHON",
}
VIDEO_RUNTIME_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024 * 1024
VIDEO_RUNTIME_MAX_ENTRIES = 250_000
VIDEO_RUNTIME_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
VIDEO_RUNTIME_MAX_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024
VIDEO_RUNTIME_MAX_COMPRESSION_RATIO = 200
VIDEO_RUNTIME_MAX_ENTRY_COMPRESSION_RATIO = 512
VIDEO_RUNTIME_MODEL_DIRECTORIES = {"checkpoint", "checkpoints", "downloads", "ffmpeg", "model", "models", "pretrained_models", "source", "sources", "weight", "weights"}
VIDEO_RUNTIME_MODEL_SUFFIXES = {".ckpt", ".engine", ".gguf", ".h5", ".hdf5", ".onnx", ".pb", ".pt", ".safetensors", ".tflite", ".weights"}
VIDEO_RUNTIME_MEDIA_SUFFIXES = {".3g2", ".3gp", ".aac", ".aif", ".aiff", ".alac", ".amr", ".asf", ".avi", ".caf", ".flac", ".flv", ".m2ts", ".m2v", ".m4a", ".m4v", ".mka", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg", ".mpg", ".mts", ".mxf", ".ogg", ".ogv", ".opus", ".vob", ".wav", ".webm", ".wma", ".wmv"}
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
    "original_client_diagnostics_api.py",
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
    "installer/component_package.py",
    "installer/component_update.py",
    "installer/configure.py",
    "installer/Create-Shortcut.ps1",
    "installer/assets/olivia.ico",
    "installer/full_patch.py",
    "installer/full-patch-manifest.json",
    "installer/Install.ps1",
    "installer/activate_private_video.py",
    "installer/mem0-capability-manifest.json",
    "installer/mem0-runtime-artifacts.json",
    "installer/mem0-runtime-requirements.txt",
    "installer/provision_mem0_embedding.py",
    "installer/runtime-requirements.txt",
    "installer/start_local.py",
    "installer/start_hidden.vbs.txt",
    "installer/uninstall.py",
    "installer/uninstall_safety.py",
    "installer/verify_mem0_runtime.py",
    "installer/version_launcher.py",
    "installer/video-capability-manifest.json",
    "installer/cosyvoice-windows-audio.patch.json",
    "installer/latentsync-windows-memmap.patch.json",
    "installer/latentsync-windows-mp4-writer.patch.json",
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
PRIVATE_REQUIRED_PAYLOAD_FILES = {
    "installer/activate_private_video.py",
}


class SetupBuildError(RuntimeError):
    """Stable setup-build failure code."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _directory_file_records(root: Path, *, prefix: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    try:
        if not root.is_dir() or _is_reparse_point(root):
            raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            if _is_reparse_point(current_path):
                raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
            for name in directories:
                if _is_reparse_point(current_path / name):
                    raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
            for name in filenames:
                path = current_path / name
                if not path.is_file() or _is_reparse_point(path):
                    raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
                relative = path.relative_to(root).as_posix()
                records.append(
                    {
                        "path": f"{prefix}/{relative}",
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
        return sorted(records, key=lambda record: str(record["path"]).casefold())
    except SetupBuildError:
        raise
    except OSError as exc:
        raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID") from exc


def _tree_sha256(records: list[dict[str, object]]) -> str:
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _voice_reference_metadata(path: Path) -> dict[str, object]:
    try:
        with wave.open(os.fspath(path), "rb") as source:
            metadata = {
                "channels": source.getnchannels(),
                "sample_width_bytes": source.getsampwidth(),
                "sample_rate_hz": source.getframerate(),
                "frame_count": source.getnframes(),
                "compression_type": source.getcomptype(),
            }
            frames = source.readframes(int(metadata["frame_count"]))
            expected_size = (
                int(metadata["frame_count"])
                * int(metadata["channels"])
                * int(metadata["sample_width_bytes"])
            )
            if len(frames) != expected_size:
                raise SetupBuildError("SETUP_VOICE_REFERENCE_TRUNCATED")
    except (EOFError, OSError, wave.Error) as exc:
        raise SetupBuildError("SETUP_VOICE_REFERENCE_INVALID") from exc
    if (
        metadata["compression_type"] != "NONE"
        or not 1 <= int(metadata["channels"]) <= 8
        or not 1 <= int(metadata["sample_width_bytes"]) <= 4
        or not 8_000 <= int(metadata["sample_rate_hz"]) <= 192_000
        or int(metadata["frame_count"]) < 1
    ):
        raise SetupBuildError("SETUP_VOICE_REFERENCE_INVALID")
    return metadata


def _video_runtime_relative(value: object) -> str:
    try:
        return _validate_relative_path(value)
    except ComponentUpdateError as exc:
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID") from exc


def _video_runtime_sha(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    return value


def _video_runtime_upstream(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and "?" not in value
            and "#" not in value
            and re.search(r"\s", value) is None
        )
    except ValueError:
        return False


def _video_runtime_source_allowed(relative: str, content: bytes, size: int) -> bool:
    logical = PurePosixPath(relative)
    suffix = logical.suffix.casefold()
    header = content[:377]
    media_magic = header.startswith((b"fLaC", b"OggS", b"caff", b"ID3", b"#!AMR", b"FLV\x01", b"\x1a\x45\xdf\xa3", b"\x30\x26\xb2\x75\x8e\x66\xcf\x11")) or (header[:4] in {b"RIFF", b"FORM"} and header[8:12] in {b"WAVE", b"AIFF", b"AIFC", b"AVI "}) or (len(header) > 1 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0) or header[4:8] == b"ftyp" or (len(header) > 376 and header[0] == header[188] == header[376] == 0x47)
    if suffix in VIDEO_RUNTIME_MEDIA_SUFFIXES or media_magic:
        return False
    parents = tuple(part.casefold() for part in logical.parent.parts)
    runtime_bin = suffix == ".bin" and parents[-3:] in {
        ("site-packages", "sentencepiece", "package_data"),
        ("site-packages", "chardet", "models"),
    }
    package_code = (
        "site-packages" in parents
        and not any(
            part in VIDEO_RUNTIME_MODEL_DIRECTORIES
            for part in parents[: parents.index("site-packages")]
        )
        and suffix in {".py", ".pyc", ".pyi", ".pyx", ".yaml", ".yml"}
    )
    pth_config = False
    if suffix == ".pth" and size <= 64 * 1024 and logical.parent.name.casefold() == "site-packages":
        try:
            text = content.decode("utf-8")
            pth_config = len(content) == size and bool(text) and all(character >= " " or character in "\t\r\n" for character in text) and not header.startswith((b"PK\x03\x04", b"GGUF", b"\x89HDF", b"\x80"))
        except UnicodeError:
            pass
    return not ((any(part in VIDEO_RUNTIME_MODEL_DIRECTORIES for part in parents) and not (runtime_bin or package_code)) or suffix in VIDEO_RUNTIME_MODEL_SUFFIXES or (suffix == ".bin" and not runtime_bin) or (suffix == ".pth" and not pth_config))


def _validate_video_runtime_build_inputs(
    value: object,
    runtime_files: dict[str, tuple[str, int, str]],
    environment: dict[str, object],
) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "components"}
        or value.get("schema_version") != "olivia.video-runtime-build-inputs.v1"
        or not isinstance(value.get("components"), dict)
        or set(value["components"]) != set(VIDEO_RUNTIME_COMPONENT_ENVIRONMENT)
    ):
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    expected_runtime_files: set[str] = set()
    required_item_keys = {
        "upstream",
        "revision",
        "tree_sha256",
        "dependencies",
        "license",
        "notice",
        "files",
    }
    for component, environment_key in VIDEO_RUNTIME_COMPONENT_ENVIRONMENT.items():
        item = value["components"][component]
        if not isinstance(item, dict) or set(item) != required_item_keys:
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        upstream = item["upstream"]
        revision = item["revision"]
        dependencies = item["dependencies"]
        if (
            not _video_runtime_upstream(upstream)
            or not isinstance(revision, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision) is None
            or not isinstance(dependencies, list)
            or not dependencies
            or len(dependencies)
            != len({dependency.casefold() for dependency in dependencies if isinstance(dependency, str)})
            or not all(
                isinstance(dependency, str)
                and re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9+_.-]+", dependency)
                for dependency in dependencies
            )
        ):
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        source_files = item["files"]
        if not isinstance(source_files, list) or not source_files:
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        normalized_files: list[dict[str, object]] = []
        source_index: dict[str, tuple[int, str]] = {}
        for raw in source_files:
            if not isinstance(raw, dict) or set(raw) != {"path", "size_bytes", "sha256"}:
                raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
            relative = _video_runtime_relative(raw.get("path"))
            size = raw.get("size_bytes")
            digest = _video_runtime_sha(raw.get("sha256"))
            folded = relative.casefold()
            if type(size) is not int or size < 0 or folded in source_index:
                raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
            source_index[folded] = (size, digest)
            normalized_files.append(
                {"path": relative, "size_bytes": size, "sha256": digest}
            )
            runtime_relative = f"{component}/runtime/{relative}"
            runtime_record = runtime_files.get(runtime_relative.casefold())
            if runtime_record != (runtime_relative, size, digest):
                raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
            expected_runtime_files.add(runtime_relative.casefold())
        if normalized_files != sorted(
            normalized_files, key=lambda record: str(record["path"]).casefold()
        ):
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        tree_digest = hashlib.sha256(
            json.dumps(normalized_files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if tree_digest != _video_runtime_sha(item["tree_sha256"]):
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        for legal_key, names in (("license", ("license", "copying")), ("notice", ("notice",))):
            legal = item[legal_key]
            if not isinstance(legal, dict) or set(legal) != {"path", "sha256"}:
                raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
            legal_path = _video_runtime_relative(legal.get("path"))
            legal_digest = _video_runtime_sha(legal.get("sha256"))
            if (
                not Path(legal_path).name.casefold().startswith(names)
                or source_index.get(legal_path.casefold(), (None, None))[1] != legal_digest
            ):
                raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
            parts = tuple(part.casefold() for part in PurePosixPath(legal_path).parts)
            distribution = next((part for part in parts if part.endswith(".dist-info")), "")
            dedicated = parts[:3] == ("site-packages", "olivia_upstream", component)
            packaged = (
                parts[:1] == ("site-packages",)
                and parts[-2:-1] == ("licenses",)
                and component.replace("-", "_") in distribution.replace("-", "_")
            )
            if legal_key == "license" and not (dedicated or packaged):
                raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        raw_environment_path = environment.get(environment_key)
        environment_path = _video_runtime_relative(raw_environment_path)
        expected_prefix = f"{component}/runtime/"
        source_environment_path = environment_path[len(expected_prefix) :]
        if (
            not environment_path.startswith(expected_prefix)
            or source_environment_path not in {"python.exe", "python/python.exe"}
            or source_environment_path.casefold() not in source_index
        ):
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    if expected_runtime_files != set(runtime_files):
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")


def _video_runtime_metadata(path: Path) -> dict[str, object]:
    try:
        physical_size = path.stat().st_size
    except OSError as exc:
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID") from exc
    if physical_size < 1 or physical_size > VIDEO_RUNTIME_MAX_ARCHIVE_BYTES:
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            expanded = sum(entry.file_size for entry in entries)
            excessive_ratio = any(entry.file_size / max(1, entry.compress_size) > VIDEO_RUNTIME_MAX_ENTRY_COMPRESSION_RATIO for entry in entries)
            if len(entries) > VIDEO_RUNTIME_MAX_ENTRIES or any(entry.flag_bits & 1 for entry in entries) or expanded > VIDEO_RUNTIME_MAX_EXPANDED_BYTES or excessive_ratio or expanded / physical_size > VIDEO_RUNTIME_MAX_COMPRESSION_RATIO:
                raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
            archive_files: dict[str, tuple[str, zipfile.ZipInfo]] = {}
            seen_entries: set[str] = set()
            for entry in entries:
                raw = entry.filename[:-1] if entry.is_dir() and entry.filename.endswith("/") else entry.filename
                relative = _video_runtime_relative(raw)
                folded = relative.casefold()
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if folded in seen_entries or mode == stat.S_IFLNK:
                    raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
                seen_entries.add(folded)
                if not entry.is_dir():
                    archive_files[folded] = (relative, entry)
            manifest_entry = archive_files.get("runtime-manifest.json")
            if (
                manifest_entry is None
                or manifest_entry[0] != "runtime-manifest.json"
                or manifest_entry[1].file_size > VIDEO_RUNTIME_MAX_MANIFEST_BYTES
            ):
                raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
            manifest = json.loads(archive.read(manifest_entry[1]))
    except SetupBuildError:
        raise
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID") from exc
    schema_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    expected_manifest_keys = {"schema_version", "version", "environment", "files"}
    if schema_version == "olivia.video-runtime-root.v2":
        if any(entry.compress_type != zipfile.ZIP_DEFLATED for _relative, entry in archive_files.values()):
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        expected_manifest_keys.add("build_inputs")
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected_manifest_keys
        or schema_version not in {
            "olivia.video-runtime-root.v1",
            "olivia.video-runtime-root.v2",
        }
        or not isinstance(manifest.get("version"), str)
        or not 1 <= len(manifest["version"]) <= 64
        or (
            schema_version == "olivia.video-runtime-root.v2"
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", manifest["version"])
            is None
        )
    ):
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    environment = manifest.get("environment")
    if not isinstance(environment, dict) or set(environment) != VIDEO_RUNTIME_ENVIRONMENT_KEYS:
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    environment_paths: set[str] = set()
    for raw_relative in environment.values():
        relative = _video_runtime_relative(raw_relative)
        environment_paths.add(relative.casefold())
        archived = archive_files.get(relative.casefold())
        if archived is None or archived[0] != relative or archived[1].file_size < 1:
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    if len(environment_paths) != len(VIDEO_RUNTIME_ENVIRONMENT_KEYS):
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    declared: set[str] = set()
    expected_hashes: dict[str, str] = {}
    runtime_files: dict[str, tuple[str, int, str]] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        relative = _video_runtime_relative(item.get("path"))
        size = item.get("size_bytes")
        digest = item.get("sha256")
        archived = archive_files.get(relative.casefold())
        entry = None if archived is None or archived[0] != relative else archived[1]
        folded = relative.casefold()
        if (
            folded == "runtime-manifest.json"
            or entry is None
            or entry.is_dir()
            or type(size) is not int
            or size < 0
            or entry.file_size != size
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or folded in declared
        ):
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        declared.add(folded)
        expected_hashes[folded] = digest
        runtime_files[folded] = (relative, size, digest)
    if (
        not environment_paths.issubset(declared)
        or set(archive_files) != declared | {"runtime-manifest.json"}
    ):
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    if schema_version == "olivia.video-runtime-root.v2":
        if list(runtime_files.values()) != sorted(
            runtime_files.values(), key=lambda record: record[0].casefold()
        ):
            raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
        _validate_video_runtime_build_inputs(
            manifest.get("build_inputs"), runtime_files, environment
        )
    try:
        with zipfile.ZipFile(path) as archive:
            for folded, expected in expected_hashes.items():
                digest = hashlib.sha256()
                inspected = bytearray()
                with archive.open(archive_files[folded][0]) as source:
                    for block in iter(lambda: source.read(1 << 20), b""):
                        digest.update(block)
                        if len(inspected) < 64 * 1024:
                            inspected.extend(block[: 64 * 1024 - len(inspected)])
                if digest.hexdigest() != expected:
                    raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
                if schema_version == "olivia.video-runtime-root.v2":
                    runtime_relative = runtime_files[folded][0]
                    source_relative = "/".join(PurePosixPath(runtime_relative).parts[2:])
                    if not _video_runtime_source_allowed(
                        source_relative, bytes(inspected), runtime_files[folded][1]
                    ):
                        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
    except SetupBuildError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID") from exc
    return {"size_bytes": path.stat().st_size, "sha256": _sha256(path)}


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _absolute_sidecar_source(
    path: Path, *, directory: bool, error_code: str
) -> Path:
    try:
        candidate = Path(os.path.abspath(path.expanduser()))
        for current in reversed((candidate, *candidate.parents)):
            if os.path.lexists(current) and _is_reparse_point(current):
                raise SetupBuildError(error_code)
        valid = candidate.is_dir() if directory else candidate.is_file()
        if not valid:
            raise SetupBuildError(error_code)
        return candidate
    except SetupBuildError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SetupBuildError(error_code) from exc


def _assert_no_reparse_tree(root: Path, *, error_code: str) -> None:
    try:
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in (*directories, *filenames):
                if _is_reparse_point(current_path / name):
                    raise SetupBuildError(error_code)
    except SetupBuildError:
        raise
    except OSError as exc:
        raise SetupBuildError(error_code) from exc


def _copytree_without_reparse(source: Path, destination: Path) -> None:
    def reject_reparse(directory: str, names: list[str]) -> tuple[str, ...]:
        current = Path(directory)
        if any(_is_reparse_point(current / name) for name in names):
            raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
        return ()

    try:
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            ignore=reject_reparse,
        )
    except SetupBuildError:
        raise
    except OSError as exc:
        raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID") from exc


def _video_offline_metadata(
    manifest_path: Path,
    root: Path,
    *,
    file_records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload = json.loads(manifest_bytes.decode("utf-8"))
        physical_root = _absolute_sidecar_source(
            root, directory=True, error_code="SETUP_VIDEO_OFFLINE_INVALID"
        )
        bundles = payload.get("bundles") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "olivia.video-capability-bom.v1"
            or not isinstance(payload.get("version"), str)
            or not payload["version"]
            or not isinstance(bundles, list)
        ):
            raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
        expected: dict[str, tuple[str, int, str]] = {}
        seen_bundles: set[str] = set()
        for bundle in bundles:
            if not isinstance(bundle, dict):
                raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
            bundle_id = bundle.get("id")
            files = bundle.get("files")
            if (
                bundle_id not in {"ordinary_video", "music_video"}
                or bundle_id in seen_bundles
                or not isinstance(files, list)
                or not files
            ):
                raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
            seen_bundles.add(bundle_id)
            for item in files:
                if not isinstance(item, dict):
                    raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
                try:
                    relative = _validate_relative_path(item.get("path"))
                except ComponentUpdateError as exc:
                    raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID") from exc
                size = item.get("size_bytes")
                digest = item.get("sha256")
                path = f"{bundle_id}/{relative}"
                folded = path.casefold()
                if (
                    type(size) is not int
                    or size < 1
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or folded in expected
                ):
                    raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
                expected[folded] = (path, size, digest)
        if seen_bundles != {"ordinary_video", "music_video"}:
            raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")

        actual_files: dict[str, Path] = {}
        actual_directories: set[str] = set()
        for current, directories, filenames in os.walk(physical_root, followlinks=False):
            current_path = Path(current)
            if _is_reparse_point(current_path):
                raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
            for name in directories:
                directory = current_path / name
                if _is_reparse_point(directory):
                    raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
                relative = directory.relative_to(physical_root).as_posix()
                actual_directories.add(relative.casefold())
            for name in filenames:
                path = current_path / name
                if not path.is_file() or _is_reparse_point(path):
                    raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
                relative = path.relative_to(physical_root).as_posix()
                folded = relative.casefold()
                if folded in actual_files:
                    raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
                actual_files[folded] = path
        expected_directories = {
            parent.as_posix().casefold()
            for path, _, _ in expected.values()
            for parent in PurePosixPath(path).parents
            if parent.as_posix() != "."
        }
        if set(actual_files) != set(expected) or actual_directories - expected_directories:
            raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
        total = 0
        for folded, (relative, size, digest) in expected.items():
            path = actual_files[folded]
            if path.stat().st_size != size or _sha256(path) != digest:
                raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID")
            total += size
            if file_records is not None:
                file_records.append(
                    {
                        "path": f"{VIDEO_OFFLINE_SIDECAR_NAME}/{relative}",
                        "size_bytes": size,
                        "sha256": digest,
                    }
                )
        if file_records is not None:
            file_records.sort(key=lambda record: str(record["path"]).casefold())
        return {
            "manifest_version": payload["version"],
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "file_count": len(expected),
            "size_bytes": total,
        }
    except SetupBuildError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupBuildError("SETUP_VIDEO_OFFLINE_INVALID") from exc


def _verify_pinned_private_sidecars(
    payload: Path, runtime: Path, video_offline_root: Path
) -> tuple[dict[str, object], list[dict[str, object]]]:
    try:
        private_manifest = json.loads(
            (payload / "offline" / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        expected_runtime = private_manifest["video_runtime"]
        expected_offline = private_manifest["video_offline"]
        if _is_reparse_point(runtime):
            raise SetupBuildError("SETUP_PRIVATE_SIDECAR_CHANGED")
        actual_runtime = {
            "path": VIDEO_RUNTIME_SIDECAR_NAME,
            "size_bytes": runtime.stat().st_size,
            "sha256": _sha256(runtime),
        }
        offline_file_records: list[dict[str, object]] = []
        actual_offline = {
            "path": VIDEO_OFFLINE_SIDECAR_NAME,
            **_video_offline_metadata(
                payload / "installer" / "video-capability-manifest.json",
                video_offline_root,
                file_records=offline_file_records,
            ),
        }
        if actual_runtime != expected_runtime or actual_offline != expected_offline:
            raise SetupBuildError("SETUP_PRIVATE_SIDECAR_CHANGED")
        return (
            {
                **actual_runtime,
                "path": os.fspath(runtime),
            },
            offline_file_records,
        )
    except SetupBuildError as exc:
        if str(exc) == "SETUP_PRIVATE_SIDECAR_CHANGED":
            raise
        raise SetupBuildError("SETUP_PRIVATE_SIDECAR_CHANGED") from exc
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise SetupBuildError("SETUP_PRIVATE_SIDECAR_CHANGED") from exc


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
    if any(
        key in manifest
        for key in ("distribution", "voice_reference", "video_runtime", "video_offline")
    ):
        raise SetupBuildError("SETUP_INPUT_VOICE_REFERENCE_FORBIDDEN")

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
    distribution: str = "public",
    voice_reference: Path | None = None,
    video_runtime: Path | None = None,
    video_offline_root: Path | None = None,
    validate_schema: bool = True,
) -> None:
    source = source.expanduser().resolve()
    offline = offline.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if distribution not in {"public", "private"}:
        raise SetupBuildError("SETUP_DISTRIBUTION_INVALID")
    if voice_reference is not None and distribution != "private":
        raise SetupBuildError("SETUP_VOICE_REFERENCE_PRIVATE_ONLY")
    if distribution == "private" and voice_reference is None:
        raise SetupBuildError("SETUP_PRIVATE_VOICE_REFERENCE_REQUIRED")
    if video_runtime is not None and distribution != "private":
        raise SetupBuildError("SETUP_VIDEO_RUNTIME_PRIVATE_ONLY")
    if distribution == "private" and video_runtime is None:
        raise SetupBuildError("SETUP_PRIVATE_VIDEO_RUNTIME_REQUIRED")
    if video_offline_root is not None and distribution != "private":
        raise SetupBuildError("SETUP_VIDEO_OFFLINE_PRIVATE_ONLY")
    if distribution == "private" and video_offline_root is None:
        raise SetupBuildError("SETUP_PRIVATE_VIDEO_OFFLINE_REQUIRED")
    if destination.exists():
        raise SetupBuildError("SETUP_PAYLOAD_EXISTS")
    _load_and_verify_manifest(source, offline, validate_schema=validate_schema)
    tracked = _git_tracked_files(source)
    if not REQUIRED_PAYLOAD_FILES.issubset(tracked):
        raise SetupBuildError("SETUP_REQUIRED_PAYLOAD_MISSING")
    if (
        distribution == "private"
        and not PRIVATE_REQUIRED_PAYLOAD_FILES.issubset(tracked)
    ):
        raise SetupBuildError("SETUP_REQUIRED_PAYLOAD_MISSING")
    selected = sorted(relative for relative in tracked if _is_release_file(relative))
    if any(
        PurePosixPath(relative).suffix.lower() in FORBIDDEN_MEDIA_SUFFIXES
        for relative in selected
    ):
        raise SetupBuildError("SETUP_TRACKED_MEDIA_FORBIDDEN")
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
        if (
            voice_reference is not None
            and video_runtime is not None
            and video_offline_root is not None
        ):
            reference = _absolute_sidecar_source(
                voice_reference,
                directory=False,
                error_code="SETUP_VOICE_REFERENCE_INVALID",
            )
            if reference.stat().st_size < 1:
                raise SetupBuildError("SETUP_VOICE_REFERENCE_INVALID")
            target = staging / "offline" / Path(*VOICE_REFERENCE_PATH.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(reference, target)
            runtime_source = _absolute_sidecar_source(
                video_runtime,
                directory=False,
                error_code="SETUP_VIDEO_RUNTIME_INVALID",
            )
            if runtime_source.stat().st_size < 1:
                raise SetupBuildError("SETUP_VIDEO_RUNTIME_INVALID")
            runtime_metadata = _video_runtime_metadata(runtime_source)
            video_offline_metadata = _video_offline_metadata(
                staging / "installer" / "video-capability-manifest.json",
                video_offline_root,
            )
            manifest_path = staging / "offline" / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["distribution"] = "private"
            manifest["voice_reference"] = {
                "path": VOICE_REFERENCE_PATH,
                "size_bytes": target.stat().st_size,
                "sha256": _sha256(target),
                "wave": _voice_reference_metadata(target),
            }
            manifest["video_runtime"] = {
                "path": VIDEO_RUNTIME_PATH,
                **runtime_metadata,
            }
            manifest["video_offline"] = {
                "path": VIDEO_OFFLINE_SIDECAR_NAME,
                **video_offline_metadata,
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
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
    distribution: str = "public",
    voice_reference: Path | None = None,
    video_runtime: Path | None = None,
    video_offline_root: Path | None = None,
) -> dict[str, object]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    setup = output / SETUP_NAME
    checksum = output / f"{SETUP_NAME}.sha256"
    sidecar = output / VIDEO_RUNTIME_SIDECAR_NAME
    offline_sidecar = output / VIDEO_OFFLINE_SIDECAR_NAME
    receipt = output / PRIVATE_RECEIPT_NAME
    if (
        any(output.glob("Olivia-Setup-x64*"))
        or sidecar.exists()
        or offline_sidecar.exists()
        or receipt.exists()
    ):
        raise SetupBuildError("SETUP_OUTPUT_EXISTS")
    payload = output / f".setup-payload-{uuid.uuid4().hex}"
    sidecar_staging = output / f".{VIDEO_RUNTIME_SIDECAR_NAME}.{uuid.uuid4().hex}.tmp"
    offline_staging = output / f".{VIDEO_OFFLINE_SIDECAR_NAME}.{uuid.uuid4().hex}.tmp"
    compiler = _find_iscc(iscc)
    completed = False
    try:
        if video_runtime is not None:
            runtime_source = _absolute_sidecar_source(
                video_runtime,
                directory=False,
                error_code="SETUP_VIDEO_RUNTIME_INVALID",
            )
            shutil.copy2(runtime_source, sidecar_staging)
            os.replace(sidecar_staging, sidecar)
        if video_offline_root is not None:
            offline_source = _absolute_sidecar_source(
                video_offline_root,
                directory=True,
                error_code="SETUP_VIDEO_OFFLINE_INVALID",
            )
            _assert_no_reparse_tree(
                offline_source, error_code="SETUP_VIDEO_OFFLINE_INVALID"
            )
            _copytree_without_reparse(offline_source, offline_staging)
            os.replace(offline_staging, offline_sidecar)
        prepare_setup_payload(
            source,
            offline,
            payload,
            distribution=distribution,
            voice_reference=voice_reference,
            video_runtime=sidecar if video_runtime is not None else None,
            video_offline_root=(
                offline_sidecar if video_offline_root is not None else None
            ),
        )
        command = [
            os.fspath(compiler),
            f"/DPayloadRoot={payload}",
            f"/DOutputDir={output}",
            f"/DAppVersion={version}",
            os.fspath(source / "installer" / "windows_setup.iss"),
        ]
        if video_runtime is not None:
            command.insert(-1, "/DPrivatePayload=1")
        result = subprocess.run(command, check=False, timeout=900)
        if result.returncode != 0 or not setup.is_file():
            raise SetupBuildError("SETUP_COMPILE_FAILED")
        compiled_artifacts = sorted(
            path for path in output.glob("Olivia-Setup-x64*")
            if path.is_file() and path != checksum
        )
        if compiled_artifacts != [setup]:
            raise SetupBuildError("SETUP_COMPILE_FAILED")
        verified_sidecars = None
        if video_runtime is not None and video_offline_root is not None:
            verified_sidecars = _verify_pinned_private_sidecars(
                payload, sidecar, offline_sidecar
            )
        artifacts = [setup]
        file_records = [
            {"path": os.fspath(path), "size_bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in artifacts
        ]
        if video_runtime is not None:
            artifacts.append(sidecar)
            file_records.append(
                verified_sidecars[0]
                if verified_sidecars is not None
                else {
                    "path": os.fspath(sidecar),
                    "size_bytes": sidecar.stat().st_size,
                    "sha256": _sha256(sidecar),
                }
            )
        if video_offline_root is not None:
            offline_file_records = (
                verified_sidecars[1]
                if verified_sidecars is not None
                else _directory_file_records(
                    offline_sidecar, prefix=VIDEO_OFFLINE_SIDECAR_NAME
                )
            )
            offline_size = sum(int(record["size_bytes"]) for record in offline_file_records)
            file_records.extend(offline_file_records)
            artifacts.append(offline_sidecar)
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": "olivia.private-setup-receipt.v1",
                        "distribution": "private",
                        "version": version,
                        "offline_root": VIDEO_OFFLINE_SIDECAR_NAME,
                        "files": [
                            {
                                **record,
                                "path": (
                                    Path(str(record["path"])).name
                                    if Path(str(record["path"])).is_absolute()
                                    else record["path"]
                                ),
                            }
                            for record in file_records
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            file_records.append(
                {
                    "path": PRIVATE_RECEIPT_NAME,
                    "size_bytes": receipt.stat().st_size,
                    "sha256": _sha256(receipt),
                }
            )
            records = [
                *(
                    record
                    for record in file_records
                    if Path(str(record["path"])).is_absolute()
                ),
                {
                    "path": os.fspath(offline_sidecar),
                    "size_bytes": offline_size,
                    "sha256": _tree_sha256(offline_file_records),
                },
            ]
        else:
            records = file_records
        artifacts.sort()
        digest = next(record["sha256"] for record in records if Path(record["path"]) == setup)
        checksum.write_text(
            "".join(
                f"{record['sha256']}  "
                f"{Path(str(record['path'])).name if Path(str(record['path'])).is_absolute() else record['path']}\n"
                for record in file_records
            ),
            encoding="ascii",
        )
        result = {
            "status": "OK",
            "setup": os.fspath(setup),
            "size_bytes": setup.stat().st_size,
            "sha256": digest,
            "artifacts": records,
        }
        completed = True
        return result
    except SetupBuildError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupBuildError("SETUP_BUILD_FAILED") from exc
    finally:
        shutil.rmtree(payload, ignore_errors=True)
        sidecar_staging.unlink(missing_ok=True)
        shutil.rmtree(offline_staging, ignore_errors=True)
        if not completed:
            cleanup_failed = False
            for artifact in (*output.glob("Olivia-Setup-x64*"), sidecar, receipt):
                try:
                    artifact.unlink(missing_ok=True)
                except OSError:
                    cleanup_failed = True
            try:
                if offline_sidecar.exists():
                    shutil.rmtree(offline_sidecar)
            except OSError:
                cleanup_failed = True
            if cleanup_failed:
                raise SetupBuildError("SETUP_OUTPUT_CLEANUP_FAILED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build-windows-setup")
    parser.add_argument("--source", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--offline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--distribution", choices=("public", "private"), default="public")
    parser.add_argument("--voice-reference", type=Path)
    parser.add_argument("--video-runtime", type=Path)
    parser.add_argument("--video-offline-root", type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_windows_setup(
            args.source,
            args.offline,
            args.output,
            version=args.version,
            iscc=args.iscc,
            distribution=args.distribution,
            voice_reference=args.voice_reference,
            video_runtime=args.video_runtime,
            video_offline_root=args.video_offline_root,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except SetupBuildError as exc:
        print(json.dumps({"status": "ERROR", "code": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
