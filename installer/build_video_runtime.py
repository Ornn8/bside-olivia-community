"""Build the private portable-Python closure used by video replies."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

from video_capability_install import (
    VideoCapabilityError,
    _load_runtime_root_manifest,
    _reject_reparse_tree,
    _safe_relative,
    _safe_sha,
    _sha256_file,
    write_runtime_root_manifest,
)

class VideoRuntimeBuildError(ValueError):
    """Stable fail-closed build error."""

_COMPONENT_ENVIRONMENT = {
    "cosyvoice": "OLIVIA_COSYVOICE_PYTHON",
    "latentsync": "OLIVIA_LATENTSYNC_PYTHON",
    "minimax": "OLIVIA_MINIMAX_COMFY_PYTHON",
    "roformer": "OLIVIA_ROFORMER_PYTHON",
}
_COMPONENT_IMPORTS = {
    "cosyvoice": ("torch", "torchaudio", "cosyvoice.cli.cosyvoice"),
    "latentsync": ("torch", "diffusers", "latentsync"),
    "minimax": ("torch", "comfy", "comfy_extras.nodes_minimax_music"),
    "roformer": ("torch", "mel_band_roformer.inference"),
}
_BOM_SCHEMA = "olivia.video-runtime-build-inputs.v1"
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MODEL_DIRECTORIES = {"checkpoint", "checkpoints", "downloads", "ffmpeg", "model", "models", "pretrained_models", "source", "sources", "weight", "weights"}
_MODEL_SUFFIXES = {".ckpt", ".engine", ".gguf", ".h5", ".hdf5", ".onnx", ".pb", ".pt", ".safetensors", ".tflite", ".weights"}
_AUDIO_SUFFIXES = {".aac", ".aif", ".aiff", ".alac", ".amr", ".caf", ".flac", ".m4a", ".mka", ".mp3", ".ogg", ".opus", ".wav", ".webm", ".wma"}
_MAX_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024

def _load_bom(path: Path | None) -> dict[str, object]:
    if path is None:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_REQUIRED")
    try:
        if not isinstance(path, Path) or not path.is_absolute() or not path.is_file():
            raise OSError
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_INVALID") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "components"} or payload.get("schema_version") != _BOM_SCHEMA or not isinstance(payload.get("components"), dict):
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_INVALID")
    return payload

def _checked_item(root: Path, item: object) -> tuple[int, list[dict[str, object]]]:
    required = {"upstream", "revision", "tree_sha256", "dependencies", "license", "notice", "files"}
    try:
        if not isinstance(item, dict) or set(item) != required:
            raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_INVALID")
        upstream, revision, dependencies = item["upstream"], item["revision"], item["dependencies"]
        if not isinstance(upstream, str) or re.fullmatch(r"https://[^/@\s]+(?:/[^\s]*)?", upstream) is None or not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
            raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_INVALID")
        if not isinstance(dependencies, list) or not dependencies or len(dependencies) != len({value.casefold() for value in dependencies if isinstance(value, str)}) or not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9+_.-]+", value) for value in dependencies):
            raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_INVALID")
        legal: list[tuple[str, str]] = []
        for key, names in (("license", ("license", "copying")), ("notice", ("notice",))):
            meta = item[key]
            if not isinstance(meta, dict) or set(meta) != {"path", "sha256"}:
                raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_INVALID")
            relative, digest = _safe_relative(meta["path"]), _safe_sha(meta["sha256"])
            if not Path(relative).name.casefold().startswith(names):
                raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_INVALID")
            legal.append((relative, digest))
    except (KeyError, TypeError, VideoCapabilityError) as exc:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_INVALID") from exc

    source_roots = (root,) if (root / "python.exe").is_file() else (root / "python", root / "site-packages")
    try:
        sources = {path for source_root in source_roots for path in source_root.rglob("*") if path.is_file()}
        sources.update(root / relative for relative, _digest in legal)
        files: list[dict[str, object]] = []
        for path in sorted(sources, key=lambda value: value.relative_to(root).as_posix().casefold()):
            relative = path.relative_to(root).as_posix()
            size, digest = _sha256_file(path)
            suffix = path.suffix.casefold()
            with path.open("rb") as stream:
                header = stream.read(16)
            audio_magic = header.startswith((b"fLaC", b"OggS", b"caff", b"ID3", b"#!AMR", b"\x30\x26\xb2\x75\x8e\x66\xcf\x11")) or (header[:4] in {b"RIFF", b"FORM"} and header[8:12] in {b"WAVE", b"AIFF", b"AIFC"}) or (len(header) > 1 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0) or (header[4:8] == b"ftyp" and header[8:12] in {b"M4A ", b"M4B ", b"M4P "})
            if suffix in _AUDIO_SUFFIXES or audio_magic:
                raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_PRIVATE_MEDIA_FORBIDDEN")
            pth_config = False
            if suffix == ".pth" and size <= 64 * 1024 and Path(relative).parent.name.casefold() == "site-packages" and not header.startswith((b"PK\x03\x04", b"GGUF", b"\x89HDF", b"\x80")):
                try:
                    text = path.read_text(encoding="utf-8")
                    pth_config = bool(text) and all(character >= " " or character in "\t\r\n" for character in text)
                except UnicodeError:
                    pass
            parents = tuple(part.casefold() for part in Path(relative).parent.parts)
            runtime_bin = suffix == ".bin" and parents[-3:] in {("site-packages", "sentencepiece", "package_data"), ("site-packages", "chardet", "models")}
            if (any(part.casefold() in _MODEL_DIRECTORIES for part in Path(relative).parts[:-1]) and not runtime_bin) or suffix in _MODEL_SUFFIXES or (suffix == ".bin" and not runtime_bin) or (suffix == ".pth" and not pth_config):
                raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BUNDLE_CONTENT_FORBIDDEN")
            files.append({"path": relative, "size_bytes": size, "sha256": digest})
    except OSError as exc:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_MISMATCH") from exc
    if not files or len(files) != len({str(value["path"]).casefold() for value in files}):
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_INVALID")
    tree = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    indexed = {str(value["path"]): str(value["sha256"]) for value in files}
    try:
        matches = files == item["files"] and tree == _safe_sha(item["tree_sha256"]) and all(indexed[path] == digest for path, digest in legal)
    except (KeyError, TypeError, VideoCapabilityError):
        matches = False
    if not matches:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_BOM_MISMATCH")
    return sum(int(value["size_bytes"]) for value in files), files

def _checked_inputs(version: str, output: Path, roots: Mapping[str, Path], bom: Mapping[str, object]) -> tuple[Path, dict[str, Path]]:
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_VERSION_INVALID")
    if not isinstance(output, Path) or not output.is_absolute():
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_OUTPUT_INVALID")
    components = bom["components"]
    if not isinstance(components, dict) or set(roots) != set(_COMPONENT_ENVIRONMENT) or set(components) != set(_COMPONENT_ENVIRONMENT):
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_COMPONENTS_INVALID")
    output_root, checked, total = output.resolve(), {}, 0
    for component in _COMPONENT_ENVIRONMENT:
        raw = roots[component]
        if not isinstance(raw, Path) or not raw.is_absolute() or not raw.is_dir():
            raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_COMPONENT_INVALID")
        try:
            root = raw.resolve(strict=True)
            _reject_reparse_tree(root)
        except (OSError, VideoCapabilityError) as exc:
            raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_COMPONENT_INVALID") from exc
        if output_root == root or root in output_root.parents:
            raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_OUTPUT_INVALID")
        if not ((root / "python.exe").is_file() or ((root / "python" / "python.exe").is_file() and (root / "site-packages").is_dir())):
            raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_PYTHON_MISSING")
        if root in checked.values():
            raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_COMPONENT_DUPLICATE")
        size, _inventory = _checked_item(root, components[component])
        checked[component] = root
        total += size
    if total > _MAX_EXPANDED_BYTES:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_TOO_LARGE")
    return output_root, checked

def _probe_environments(environment: Mapping[str, str], runtime_root: Path) -> bool:
    clean = {key: value for key, value in os.environ.items() if key.upper() not in {"PYTHONHOME", "PYTHONPATH"}}
    clean.update(PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1")
    try:
        for component, key in _COMPONENT_ENVIRONMENT.items():
            allowed_root = runtime_root / component / "runtime"
            script = (
                "from pathlib import Path; import importlib,sys; root=Path(sys.argv[1]).resolve(); "
                "inside=lambda value:(path:=Path(value).resolve())==root or root in path.parents; "
                "assert inside(sys.executable) and inside(sys.prefix) and inside(sys.base_prefix); assert all(inside(value) for value in sys.path if value); "
                f"[importlib.import_module(name) for name in {_COMPONENT_IMPORTS[component]!r}]; import torch; "
                "assert torch.version.cuda and torch.cuda.is_available(); assert torch.ones(1,device='cuda').is_cuda"
            )
            if subprocess.run([environment[key], "-I", "-c", script, str(allowed_root)], cwd=allowed_root, capture_output=True, check=False, timeout=30, env=clean, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode:
                return False
    except (OSError, subprocess.SubprocessError):
        return False
    return True

def _write_zip(runtime_root: Path, target: Path) -> None:
    try:
        with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for source in sorted((path for path in runtime_root.rglob("*") if path.is_file()), key=lambda path: path.relative_to(runtime_root).as_posix().casefold()):
                relative = source.relative_to(runtime_root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o100644 << 16
                with source.open("rb") as input_stream, archive.open(info, "w", force_zip64=True) as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_ARCHIVE_FAILED") from exc

def _verify_zip(target: Path, manifest_sha256: str) -> None:
    try:
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            if len(names) != len({name.casefold() for name in names}) or any(_safe_relative(name) != name for name in names):
                raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_ARCHIVE_INVALID")
            manifest_bytes = archive.read("runtime-manifest.json")
            payload = json.loads(manifest_bytes)
            expected = {item["path"]: (item["size_bytes"], item["sha256"]) for item in payload["files"]}
            if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256 or set(names) != {*expected, "runtime-manifest.json"}:
                raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_ARCHIVE_INVALID")
            for relative, expected_value in expected.items():
                digest, size = hashlib.sha256(), 0
                with archive.open(relative) as stream:
                    while chunk := stream.read(1024 * 1024):
                        size += len(chunk)
                        digest.update(chunk)
                if (size, digest.hexdigest()) != expected_value:
                    raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_ARCHIVE_INVALID")
    except VideoRuntimeBuildError:
        raise
    except (KeyError, TypeError, ValueError, OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile, VideoCapabilityError) as exc:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_ARCHIVE_INVALID") from exc

def build_video_runtime_archive(*, version: str, output_directory: Path, component_roots: Mapping[str, Path], build_input_bom: Path | None = None) -> Path:
    """Build, verify, then atomically publish one locked runtime ZIP."""

    bom = _load_bom(build_input_bom)
    output_root, checked = _checked_inputs(version, output_directory, component_roots, bom)
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / f"Olivia-video-runtime-{version}.zip"
    lock = output_root / f".{target.name}.lock"
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_LOCKED") from exc
    except OSError as exc:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_FAILED") from exc
    try:
        if target.exists():
            raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_OUTPUT_EXISTS")
        with tempfile.TemporaryDirectory(prefix=".olivia-video-runtime-", dir=output_root, ignore_cleanup_errors=True) as temporary:
            runtime_root = Path(temporary).resolve() / "root"
            runtime_root.mkdir()
            environment = {}
            for component, key in _COMPONENT_ENVIRONMENT.items():
                source = checked[component]
                relative, destination = f"{component}/runtime", runtime_root / f"{component}/runtime"
                if (source / "python.exe").is_file():
                    shutil.copytree(source, destination)
                else:
                    destination.mkdir(parents=True)
                    shutil.copytree(source / "python", destination / "python")
                    shutil.copytree(source / "site-packages", destination / "site-packages")
                    for legal in ("license", "notice"):
                        legal_path = _safe_relative(bom["components"][component][legal]["path"])
                        target_path = destination / legal_path
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source / legal_path, target_path)
                _checked_item(destination, bom["components"][component])
                executable = "python.exe" if (source / "python.exe").is_file() else "python/python.exe"
                environment[key] = f"{relative}/{executable}"
            manifest_sha = write_runtime_root_manifest(runtime_root, version=version, environment=environment, build_inputs=bom)
            verified = _load_runtime_root_manifest(runtime_root, manifest_sha, verify_files=False)
            if set(verified) != set(_COMPONENT_ENVIRONMENT.values()) or not _probe_environments(verified, runtime_root):
                raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_NOT_PORTABLE")
            staging = output_root / f".{target.name}.{uuid.uuid4().hex}.tmp"
            try:
                _write_zip(runtime_root, staging)
                _verify_zip(staging, manifest_sha)
                os.link(staging, target)
            except FileExistsError as exc:
                raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_OUTPUT_EXISTS") from exc
            finally:
                try:
                    staging.unlink(missing_ok=True)
                except OSError:
                    pass
    except VideoRuntimeBuildError:
        raise
    except (OSError, VideoCapabilityError) as exc:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_FAILED") from exc
    finally:
        try:
            os.close(lock_fd)
            lock.unlink(missing_ok=True)
        except OSError:
            pass
    return target.resolve()

class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise VideoRuntimeBuildError("VIDEO_RUNTIME_BUILD_ARGUMENTS_INVALID")

def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Build a verified private Olivia video Python runtime archive.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--build-input-bom", required=True, type=Path)
    for component in _COMPONENT_ENVIRONMENT:
        parser.add_argument(f"--{component}-root", required=True, type=Path)
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        archive = build_video_runtime_archive(
            version=arguments.version,
            output_directory=arguments.output_dir.expanduser().resolve(),
            component_roots={component: getattr(arguments, f"{component}_root").expanduser().resolve() for component in _COMPONENT_ENVIRONMENT},
            build_input_bom=arguments.build_input_bom.expanduser().resolve(),
        )
        size, digest = _sha256_file(archive)
        print(json.dumps({"status": "READY", "path": str(archive), "size_bytes": size, "sha256": digest}, sort_keys=True))
        return 0
    except VideoRuntimeBuildError as exc:
        print(json.dumps({"status": "ERROR", "code": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"status": "ERROR", "code": "VIDEO_RUNTIME_BUILD_FAILED"}, sort_keys=True), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
