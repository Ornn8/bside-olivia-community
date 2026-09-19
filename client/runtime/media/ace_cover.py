"""Portable, single-pass cover generation using installation-configured assets."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Mapping

from runtime.media.managed_subprocess import run_managed_process
from runtime.media.media_paths import configured_media_path


class CoverError(RuntimeError):
    pass


def cover_paths(environment: Mapping[str, str]) -> dict[str, Path | None]:
    paths = {name: configured_media_path(environment, key) for name, key in {
        "runtime_root": "OLIVIA_ACE_ROOT", "python": "OLIVIA_ACE_PYTHON",
        "voice_lora": "OLIVIA_ACE_VOICE_LORA", "reference": "OLIVIA_ACE_REFERENCE",
        "asr_model": "OLIVIA_ACE_ASR_MODEL", "data_root": "OLIVIA_LOCAL_DATA_ROOT",
    }.items()}
    if paths["data_root"] is not None:
        root = paths["runtime_root"] or paths["data_root"] / "capabilities" / "ace-cover"
        paths["runtime_root"] = root
        for key, relative in {"python": "runtime/python.exe", "voice_lora": "voice/checkpoint-5000",
                              "reference": "reference/reference-vocals-30s.wav", "asr_model": "checkpoints/large-v3-turbo.pt"}.items():
            if paths[key] is None:
                paths[key] = root / relative
    return paths


def cover_configured(environment: Mapping[str, str]) -> bool:
    return ace_paths_configured(cover_paths(environment))


def ace_paths_configured(paths) -> bool:
    root, lora = paths["runtime_root"], paths["voice_lora"]
    return bool(root and lora and paths["data_root"] and
                all(paths[key] and paths[key].is_file() for key in ("python", "reference")) and
                (root / "source/acestep/handler.py").is_file() and
                (root / "checkpoints/acestep-v15-xl-sft").is_dir() and
                all((lora / name).is_file() for name in ("adapter_config.json", "adapter_model.safetensors")))


def generate_cover(source: Path | None, output: Path, *, environment: Mapping[str, str],
                   lyrics: str = "", language: str = "unknown") -> dict[str, object]:
    if source is None or not source.is_file():
        raise CoverError("COVER_SOURCE_REQUIRED")
    if not cover_configured(environment):
        raise CoverError("COVER_RUNTIME_UNAVAILABLE")
    paths = cover_paths(environment)
    if not lyrics.strip() and not (paths["asr_model"] and paths["asr_model"].is_file()):
        raise CoverError("COVER_LYRICS_REQUIRED")
    return generate_ace(source, output, environment=environment, paths=paths, lyrics=lyrics, language=language)


def generate_ace(source, output, *, environment, paths, lyrics, language,
                 task_type="cover", parameters=None):
    job = output.parent / (output.stem + ("-cover-stages" if task_type == "cover" else "-original-stages"))
    job.mkdir(parents=True, exist_ok=True)
    temp = job / "tmp"
    temp.mkdir(exist_ok=True)
    partial = output.with_name(output.stem + ".partial.wav")
    partial.unlink(missing_ok=True)
    request = {key: str(value) if value else "" for key, value in paths.items()}
    from runtime.media.latentsync_reply import resolve_ffmpeg_executable
    request["ffmpeg"] = str(resolve_ffmpeg_executable(environment))
    request.update(source=str(source.resolve()) if source is not None else None,
                   output=str(partial.resolve()), lyrics=lyrics, language=language,
                   task_type=task_type, **(parameters or {}))
    def digest(path):
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    fingerprints = {"source": digest(source) if source is not None else None, "worker": digest(Path(__file__).with_name("ace_cover_worker.py"))}
    for key, asset in (("reference", paths["reference"]), ("lora", paths["voice_lora"] / "adapter_model.safetensors")):
        if asset.is_file():
            fingerprints[key] = digest(asset)
    request["fingerprints"] = fingerprints
    request_path = job / "request.private.json"
    receipt = job / "completed.private.json"
    try:
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        if previous["request"] == request and output.is_file() and digest(output) == previous["output_sha256"]:
            return {**previous["metadata"], "music_stage": "reused"}
    except (OSError, ValueError, KeyError):
        pass
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, **environment, "TEMP": str(temp), "TMP": str(temp),
           "PYTHONIOENCODING": "utf-8", "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4"}
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(key, None)
    ffmpeg = configured_media_path(environment, "OLIVIA_FFMPEG_EXE")
    if ffmpeg:
        env["PATH"] = str(ffmpeg.parent) + os.pathsep + env.get("PATH", "")
    try:
        result = run_managed_process(
            [str(paths["python"]), "-I", "-X", "utf8", str(Path(__file__).with_name("ace_cover_worker.py")), str(request_path)],
            cwd=paths["runtime_root"], env=env, timeout_seconds=1800)
        # Keep raw provider output locally, never return private paths/lyrics over status.
        (job / "worker.private.log").write_bytes(result.stdout + b"\n" + result.stderr)
        if result.returncode or not partial.is_file():
            code = "COVER_LYRICS_REQUIRED" if b"COVER_LYRICS_REQUIRED" in result.stderr else "COVER_GENERATION_FAILED"
            raise CoverError(code)
        metadata = json.loads((job / "progress.json").read_text(encoding="utf-8"))
        partial.replace(output)
        metadata.update(audio_model="ACE-Step-1.5-XL", task_type=task_type)
        receipt.write_text(json.dumps({"request": request, "metadata": metadata, "output_sha256": digest(output)}), encoding="utf-8")
        return metadata
    except subprocess.TimeoutExpired as exc:
        (job / "worker.private.log").write_bytes((exc.output or b"") + b"\n" + (exc.stderr or b""))
        raise CoverError("COVER_GENERATION_TIMEOUT") from None
    finally:
        partial.unlink(missing_ok=True)
