"""Assemble a normal video reply followed by a generated song performance."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from llm_gateway import Gateway
from runtime.media.latentsync_reply import (
    LatentSyncReplyError,
    render_latentsync_video,
    resolve_ffmpeg_executable,
)
from runtime.media.media_paths import configured_media_path
from runtime.media.managed_subprocess import run_managed_process
from runtime.media.managed_voice_reference import (
    ManagedVoiceReferenceError,
    managed_voice_reference_declared,
    resolve_managed_voice_reference,
    validate_voice_reference,
)
from runtime.media.music_duration import MUSIC_DURATION_OPTIONS, normalize_music_duration as _normalize_music_duration
from runtime.reply.reply_media import (
    ReplyMediaError,
    assemble_latentsync_video_delivery,
    render_reply_video,
)
from runtime.media.song_content import plan_song_content
from .voice_direction import VoicePerformancePlan


_MINIMAX_WORKER_TIMEOUT_SECONDS = 7500.0
_MUSIC_STAGE_MANIFEST_VERSION = 3
_RUNTIME_PROBE_TIMEOUT_SECONDS = 120.0
_RUNTIME_PROBE_REMOVED_ENVIRONMENT = (
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
)
_PROVIDER_DIAGNOSTIC_LIMIT = 512
_MEDIA_VALIDATION_TIMEOUT_SECONDS = 180.0
_BREEZE_MINIMUM_VRAM_MIB = 10 * 1024


class MusicReplyError(RuntimeError):
    """Stable product error raised when a song stage cannot complete."""

    def __init__(self, error_code: str, *, diagnostic: str = "") -> None:
        super().__init__(error_code)
        self.diagnostic = str(diagnostic)[:_PROVIDER_DIAGNOSTIC_LIMIT]


def _provider_failure_diagnostic(*, returncode: object, stderr: object) -> str:
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr or "")
    patterns = ((r"TimeoutExpired", "process timeout"), (r"CUDA out of memory|CUDNN_STATUS_ALLOC_FAILED", "CUDA out of memory"),
                (r"DLL load failed|\.dll\b.*(?:missing|not found)", "DLL load failed"), (r"No module named|ModuleNotFoundError", "Python module missing"),
                (r"(?:config|configuration).*(?:invalid|missing|not found)", "configuration unavailable"),
                (r"FileNotFoundError|No such file or directory", "file unavailable"), (r"PermissionError|Access is denied", "permission denied"))
    summary = ", ".join(label for pattern, label in patterns if re.search(pattern, text, re.I)) or "provider stderr redacted"
    return f"returncode={returncode}; stderr={summary}"[:_PROVIDER_DIAGNOSTIC_LIMIT]


def _persist_provider_failure(error_code: str, diagnostic: str, environment: Mapping[str, str] | None) -> None:
    data_root = configured_media_path(os.environ if environment is None else environment, "OLIVIA_LOCAL_DATA_ROOT")
    if data_root is None: return
    try:
        log_root = data_root / "logs"; log_root.mkdir(parents=True, exist_ok=True)
        with (log_root / "media-provider.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": int(time.time()), "error_code": error_code, "diagnostic": diagnostic[:_PROVIDER_DIAGNOSTIC_LIMIT]}, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError: return


def _provider_exception_failure(error_code: str, exc: BaseException, environment: Mapping[str, str] | None = None) -> MusicReplyError:
    chain, current = [], exc
    while current is not None and current not in chain: chain.append(current); current = current.__cause__ or current.__context__
    root = chain[-1]; category = "TimeoutExpired" if any(isinstance(item, (TimeoutError, subprocess.TimeoutExpired)) for item in chain) else type(root).__name__
    diagnostic = _provider_failure_diagnostic(returncode=getattr(root, "returncode", "unavailable"), stderr=category)
    _persist_provider_failure(error_code, diagnostic, environment)
    return MusicReplyError(error_code, diagnostic=diagnostic)


@dataclass(frozen=True)
class MusicProviderPathSnapshot:
    """Immutable provider paths resolved once for one musical render."""

    environment: Mapping[str, str]
    minimax_python: Path | None
    minimax_root: Path | None
    minimax_worker: Path | None
    roformer_executable: Path | None
    roformer_model: Path | None
    roformer_config: Path | None
    latentsync_python: Path | None
    latentsync_root: Path | None
    ffmpeg_executable: Path | None
    provider_cache_root: Path | None


def _run_runtime_probe(command: list[str], *, cwd: Path) -> bool:
    environment = dict(os.environ)
    for key in _RUNTIME_PROBE_REMOVED_ENVIRONMENT:
        environment.pop(key, None)
    for attempt in range(2):
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                check=False,
                env=environment,
                timeout=_RUNTIME_PROBE_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            if attempt == 0:
                continue
            return False
        if result.returncode == 0:
            return True
    return False


def _python_runtime_ready(
    executable: Path | None,
    *,
    cwd: Path | None,
    imports: tuple[str, ...],
    accepted_torch_versions: tuple[str, ...],
    prepend_cwd: bool = True,
) -> bool:
    if executable is None or not executable.is_file() or cwd is None or not cwd.is_dir():
        return False
    script = (
        "import importlib, os, sys; "
        "assert sys.version_info[:2] >= (3, 10); "
        f"{'sys.path.insert(0, os.getcwd()); ' if prepend_cwd else ''}"
        f"[importlib.import_module(name) for name in {imports!r}]; "
        "import torch; "
        f"assert torch.__version__ in {accepted_torch_versions!r}; "
        "assert torch.version.cuda; "
        "assert torch.cuda.is_available(); "
        "torch.ones(1, device='cuda')"
    )
    return _run_runtime_probe([str(executable), "-I", "-B", "-c", script], cwd=cwd)


def _breeze_hardware_status() -> tuple[bool, str | None]:
    """Recheck the GPU on this host; copied runtime state is never trusted."""

    executable = shutil.which("nvidia-smi")
    if executable is None:
        return False, "BREEZE_TTS_NVIDIA_GPU_REQUIRED"
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        values = [
            int(line.strip())
            for line in completed.stdout.splitlines()
            if line.strip().isdigit()
        ]
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, "BREEZE_TTS_GPU_CAPABILITY_UNVERIFIED"
    if completed.returncode != 0 or not values:
        return False, "BREEZE_TTS_GPU_CAPABILITY_UNVERIFIED"
    if values[0] < _BREEZE_MINIMUM_VRAM_MIB:
        return False, "BREEZE_TTS_10GB_VRAM_REQUIRED"
    return True, None


def require_breeze_hardware() -> None:
    ready, reason_code = _breeze_hardware_status()
    if not ready:
        raise MusicReplyError(reason_code or "BREEZE_TTS_GPU_CAPABILITY_UNVERIFIED")


def _executable_runtime_ready(executable: Path | None) -> bool:
    return bool(
        executable is not None
        and executable.is_file()
        and _run_runtime_probe([str(executable), "--help"], cwd=executable.parent)
    )


def _music_provider_path_snapshot(
    environment: Mapping[str, str] | None = None,
) -> MusicProviderPathSnapshot:
    environment = MappingProxyType(dict(os.environ if environment is None else environment))
    return MusicProviderPathSnapshot(
        environment=environment,
        minimax_python=configured_media_path(environment, "OLIVIA_MINIMAX_COMFY_PYTHON"),
        minimax_root=configured_media_path(environment, "OLIVIA_MINIMAX_COMFY_ROOT"),
        minimax_worker=configured_media_path(environment, "OLIVIA_MINIMAX_WORKER"),
        roformer_executable=(
            configured_media_path(environment, "OLIVIA_ROFORMER_PYTHON")
            or configured_media_path(environment, "OLIVIA_ROFORMER_EXE")
        ),
        roformer_model=configured_media_path(environment, "OLIVIA_ROFORMER_MODEL_PATH"),
        roformer_config=configured_media_path(environment, "OLIVIA_ROFORMER_CONFIG_PATH"),
        latentsync_python=configured_media_path(environment, "OLIVIA_LATENTSYNC_PYTHON"),
        latentsync_root=configured_media_path(environment, "OLIVIA_LATENTSYNC_ROOT"),
        ffmpeg_executable=configured_media_path(environment, "OLIVIA_FFMPEG_EXE"),
        provider_cache_root=configured_media_path(environment, "OLIVIA_PROVIDER_CACHE_ROOT"),
    )


def normalize_music_duration(value: object) -> int:
    """Return one of the two product durations; intermediate values are invalid."""

    try:
        return _normalize_music_duration(value)
    except ValueError:
        raise MusicReplyError("MUSIC_DURATION_INVALID") from None


def musical_reply_configured(
    env: Mapping[str, str],
    *,
    performance_video_path: Path | None,
) -> bool:
    """Return whether the renderer's complete musical delivery closure exists."""

    def configured_path(name: str) -> Path | None:
        return configured_media_path(env, name)

    minimax_root = configured_path("OLIVIA_MINIMAX_COMFY_ROOT")
    latentsync_root = configured_path("OLIVIA_LATENTSYNC_ROOT")
    ordinary_scene = configured_path("OLIVIA_ORDINARY_ACTION_BASE")
    transition_reference = configured_path("OLIVIA_OFFICIAL_REPLY_REFERENCE")
    delivery_paths = (
        configured_path("OLIVIA_TTS_CONFIG"),
        configured_path("OLIVIA_LOCAL_DATA_ROOT"),
    )
    if minimax_root is None or latentsync_root is None or any(
        path is None for path in delivery_paths
    ):
        return False
    try:
        delivery = assemble_latentsync_video_delivery(
            delivery_paths[0],
            delivery_paths[1],
            env,
        )
    except ReplyMediaError:
        return False
    roformer_executable = configured_path("OLIVIA_ROFORMER_PYTHON") or configured_path(
        "OLIVIA_ROFORMER_EXE"
    )
    configured_files = (
        roformer_executable,
        configured_path("OLIVIA_ROFORMER_MODEL_PATH"),
        configured_path("OLIVIA_ROFORMER_CONFIG_PATH"),
        configured_path("OLIVIA_MINIMAX_COMFY_PYTHON"),
        configured_path("OLIVIA_MINIMAX_WORKER"),
        configured_path("OLIVIA_LATENTSYNC_PYTHON"),
    )
    if (
        ordinary_scene is None
        or transition_reference is None
        or any(path is None for path in configured_files)
    ):
        return False
    required = (
        transition_reference,
        *configured_files[:6],
        minimax_root / "main.py",
        minimax_root / "comfy_extras" / "nodes_minimax_music.py",
        minimax_root / "models" / "diffusion_models" / "minimax_music3_dit_int8_convrot.safetensors",
        minimax_root / "models" / "text_encoders" / "minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
        minimax_root / "models" / "vae" / "minimax_music3_dav.safetensors",
        latentsync_root / "scripts" / "inference.py",
        latentsync_root / "configs" / "unet" / "stage2_efficient.yaml",
        latentsync_root / "checkpoints" / "latentsync_unet.pt",
    )
    return bool(
        performance_video_path is not None
        and performance_video_path.is_file()
        and ordinary_scene.is_file()
        and str(getattr(delivery.tts, "provider", "")).casefold()
        == "breeze_tts2"
        and (
            Path(delivery.tts.model_dir)
            / "drbaph_Breeze-TTS-2-comfyui"
            / "Breeze-TTS-2-int8-hybrid.safetensors"
        ).is_file()
        and minimax_root.is_dir()
        and latentsync_root.is_dir()
        and all(path.is_file() for path in required)
    )


_VIDEO_REPLY_SOURCE_URLS = {
    ("breeze_tts2", "domestic"): "https://hf-mirror.com/drbaph/Breeze-TTS-2-comfyui",
    ("breeze_tts2", "official"): "https://huggingface.co/drbaph/Breeze-TTS-2-comfyui",
    ("livetalking", "official"): "https://github.com/lipku/LiveTalking/tree/a97f01ba366e55eeed94e88d6bae38ed77b3a1b9",
    ("latentsync", "domestic"): "https://modelscope.cn/models/chenmingyu/latentsync",
    ("latentsync", "official"): "https://github.com/bytedance/LatentSync/tree/a229c3948406bc2cf6eaf4873e662e70c6a04746",
    ("minimax_music3", "domestic"): "https://modelscope.cn/models/Comfy-Org/MiniMax-Music-3",
    ("minimax_music3", "official"): "https://huggingface.co/Comfy-Org/MiniMax-Music-3/tree/6444666eb6edfb2c7fcab5f8b81da8b84b4b17b6",
    ("roformer", "domestic"): "https://hf-mirror.com/KimberleyJSN/melbandroformer",
    ("roformer", "official"): "https://huggingface.co/KimberleyJSN/melbandroformer",
    ("ffmpeg", "official"): "https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/ffmpeg-9.0.1-essentials_build.zip",
}


def video_reply_source_url(capability_id: object, source_id: object) -> str | None:
    if not isinstance(capability_id, str) or not isinstance(source_id, str):
        return None
    return _VIDEO_REPLY_SOURCE_URLS.get((capability_id, source_id))


def video_reply_dependency_status(
    env: Mapping[str, str],
    *,
    performance_video_path: Path | None,
    probe_runtime: bool = True,
) -> dict[str, object]:
    """Describe the complete speech-plus-music closure without exposing local paths."""

    def configured(name: str) -> Path | None:
        return configured_media_path(env, name)

    def file(name: str) -> bool:
        path = configured(name)
        return path is not None and path.is_file()

    tts_config = configured("OLIVIA_TTS_CONFIG")
    local_data_root = configured("OLIVIA_LOCAL_DATA_ROOT")
    delivery_ready = False
    breeze_runtime_ready = False
    breeze_hardware_ready, breeze_hardware_reason = (
        _breeze_hardware_status() if probe_runtime else (True, None)
    )
    if all(path is not None for path in (tts_config, local_data_root)):
        try:
            delivery = assemble_latentsync_video_delivery(
                tts_config,
                local_data_root,
                env,
            )
            delivery_ready = True
            external_python = Path(
                str(delivery.tts.provider_options.get("external_python", ""))
            )
            breeze_runtime_ready = not probe_runtime or _python_runtime_ready(
                external_python,
                cwd=Path(delivery.tts.runtime_root),
                imports=("torch", "transformers", "soundfile", "whisper"),
                accepted_torch_versions=("2.9.1+cu128",),
                prepend_cwd=False,
            )
        except ReplyMediaError:
            pass
    minimax_root = configured("OLIVIA_MINIMAX_COMFY_ROOT")
    minimax_ready = bool(
        minimax_root is not None
        and minimax_root.is_dir()
        and file("OLIVIA_MINIMAX_COMFY_PYTHON")
        and file("OLIVIA_MINIMAX_WORKER")
        and all(
            path.is_file()
            for path in (
                minimax_root / "main.py",
                minimax_root / "comfy_extras" / "nodes_minimax_music.py",
                minimax_root / "models" / "diffusion_models" / "minimax_music3_dit_int8_convrot.safetensors",
                minimax_root / "models" / "text_encoders" / "minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
                minimax_root / "models" / "vae" / "minimax_music3_dav.safetensors",
            )
        )
        and (
            not probe_runtime
            or _python_runtime_ready(
                configured("OLIVIA_MINIMAX_COMFY_PYTHON"),
                cwd=minimax_root,
                imports=("torch", "comfy", "comfy_extras.nodes_minimax_music"),
                accepted_torch_versions=("2.13.0+cu130",),
            )
        )
    )
    latentsync_root = configured("OLIVIA_LATENTSYNC_ROOT")
    latentsync_ready = bool(
        latentsync_root is not None
        and latentsync_root.is_dir()
        and file("OLIVIA_LATENTSYNC_PYTHON")
        and all(
            path.is_file()
            for path in (
                latentsync_root / "scripts" / "inference.py",
                latentsync_root / "configs" / "unet" / "stage2_efficient.yaml",
                latentsync_root / "checkpoints" / "latentsync_unet.pt",
            )
        )
        and (
            not probe_runtime
            or _python_runtime_ready(
                configured("OLIVIA_LATENTSYNC_PYTHON"),
                cwd=latentsync_root,
                imports=("torch", "diffusers", "latentsync"),
                accepted_torch_versions=("2.5.1+cu121", "2.9.1+cu128"),
            )
        )
    )
    roformer_python = configured("OLIVIA_ROFORMER_PYTHON")
    roformer_executable = roformer_python or configured("OLIVIA_ROFORMER_EXE")
    roformer_ready = bool(
        roformer_executable
        and roformer_executable.is_file()
        and file("OLIVIA_ROFORMER_MODEL_PATH")
        and file("OLIVIA_ROFORMER_CONFIG_PATH")
        and (
            not probe_runtime
            or (
                _python_runtime_ready(
                    roformer_python,
                    cwd=roformer_python.parent if roformer_python else None,
                    imports=("torch", "mel_band_roformer.inference"),
                    accepted_torch_versions=("2.11.0+cu128", "2.13.0+cu130"),
                )
                if roformer_python is not None
                else _executable_runtime_ready(roformer_executable)
            )
        )
    )
    ordinary_assets_ready = file("OLIVIA_ORDINARY_ACTION_BASE")
    music_assets_ready = bool(
        file("OLIVIA_OFFICIAL_REPLY_REFERENCE")
        and performance_video_path is not None
        and performance_video_path.is_file()
    )
    try:
        ffmpeg_ready = resolve_ffmpeg_executable(env).is_file()
    except LatentSyncReplyError:
        ffmpeg_ready = False
    provider_cache = configured("OLIVIA_PROVIDER_CACHE_ROOT")
    workspace_ready = bool(provider_cache is not None and provider_cache.is_absolute())
    explicit_voice = str(env.get("OLIVIA_REPLY_VOICE_REFERENCE") or "").strip()
    voice_declared = bool(explicit_voice)
    voice_ready = False
    voice_reason = "VOICE_REFERENCE_UNAVAILABLE"
    voice_install_mode = "manual" if explicit_voice else "managed"
    voice_source_summary = (
        "由 OLIVIA_REPLY_VOICE_REFERENCE 显式配置"
        if explicit_voice
        else "由提供此私有版本的安装程序管理"
    )
    if explicit_voice:
        try:
            effective_reference = configured("OLIVIA_REPLY_VOICE_REFERENCE")
            if effective_reference is None:
                raise ManagedVoiceReferenceError("VOICE_REFERENCE_UNAVAILABLE")
            validate_voice_reference(effective_reference)
            voice_ready = True
        except ManagedVoiceReferenceError as exc:
            voice_reason = str(exc)
    elif local_data_root is not None:
        voice_declared = managed_voice_reference_declared(local_data_root)
        if voice_declared:
            try:
                resolve_managed_voice_reference(local_data_root)
                voice_ready = True
            except ManagedVoiceReferenceError as exc:
                voice_reason = str(exc)

    def item(
        identifier: str,
        label: str,
        ready: bool,
        install_mode: str,
        source_summary: str,
        sources: tuple[tuple[str, str, str], ...] = (),
        reason_code: str | None = None,
    ) -> dict[str, object]:
        if identifier == "latentsync":
            source_summary = "国内：ModelScope 社区镜像；备用：GitHub / Hugging Face"
        elif identifier == "minimax_music3":
            source_summary = "国内：ModelScope；备用：Hugging Face"
        result: dict[str, object] = {
            "id": identifier,
            "label": label,
            "state": "ready" if ready else "missing",
            "install_mode": install_mode,
            "source_summary": source_summary,
            "sources": [
                {
                    "id": source_id,
                    "label": (
                        "国内源（ModelScope）"
                        if source_id == "domestic"
                        and identifier in {"latentsync", "minimax_music3"}
                        else source_label
                    ),
                }
                for source_id, source_label, _source_url in sources
            ],
        }
        if not ready and reason_code is not None:
            result["reason_code"] = reason_code
        return result

    dependencies = [
        item(
            "breeze_tts2",
            "语音合成（Breeze TTS 2）",
            delivery_ready
            and breeze_runtime_ready
            and breeze_hardware_ready
            and bool(tts_config and tts_config.is_file()),
            "automatic",
            "国内：HF-Mirror；备用：Hugging Face。模型限研究与非商业用途，实测要求 NVIDIA 10GB 及以上显存",
            (
                (
                    "domestic",
                    "国内源（HF-Mirror）",
                    "https://hf-mirror.com/drbaph/Breeze-TTS-2-comfyui",
                ),
                (
                    "official",
                    "官方源（Hugging Face）",
                    "https://huggingface.co/drbaph/Breeze-TTS-2-comfyui",
                ),
            ),
            reason_code=breeze_hardware_reason,
        ),
        item(
            "latentsync",
            "口型视频（LatentSync）",
            latentsync_ready,
            "manual",
            "国内：ByteDance Gitee + HF-Mirror；备用：GitHub / Hugging Face",
            (
                (
                    "domestic",
                    "国内源（ByteDance Gitee）",
                    "https://modelscope.cn/models/chenmingyu/latentsync",
                ),
                (
                    "official",
                    "官方源（GitHub）",
                    "https://github.com/bytedance/LatentSync/tree/a229c3948406bc2cf6eaf4873e662e70c6a04746",
                ),
            ),
        ),
        item(
            "minimax_music3",
            "音乐生成（MiniMax Music 3）",
            minimax_ready,
            "manual",
            "国内：HF-Mirror；备用：Hugging Face",
            (
                (
                    "domestic",
                    "国内源（HF-Mirror）",
                    "https://modelscope.cn/models/Comfy-Org/MiniMax-Music-3",
                ),
                (
                    "official",
                    "官方源（Hugging Face）",
                    "https://huggingface.co/Comfy-Org/MiniMax-Music-3/tree/6baad88896848433857c170ba4f05d2ea9d5f218",
                ),
            ),
        ),
        item(
            "roformer",
            "人声分离（RoFormer）",
            roformer_ready,
            "automatic",
            "国内：HF-Mirror；备用：Hugging Face",
            (
                (
                    "domestic",
                    "国内源（HF-Mirror）",
                    "https://hf-mirror.com/KimberleyJSN/melbandroformer",
                ),
                (
                    "official",
                    "官方源（Hugging Face）",
                    "https://huggingface.co/KimberleyJSN/melbandroformer",
                ),
            ),
        ),
        item(
            "official_video_assets",
            "Olivia 场景与转场素材",
            ordinary_assets_ready,
            "local_import",
            "从用户本机正版 Olivia 导入，不联网下载",
        ),
        item(
            "music_video_assets",
            "Olivia 音乐视频与转场素材",
            music_assets_ready,
            "local_import",
            "从用户本机正版 Olivia 导入，不联网下载",
        ),
        item(
            "ffmpeg",
            "媒体工具（FFmpeg）",
            ffmpeg_ready,
            "manual",
            "当前核心安装器未包含；可使用系统 FFmpeg 或后续受管包",
            (
                (
                    "official",
                    "官方 Python 包（PyPI）",
                    "https://pypi.org/project/imageio-ffmpeg/0.6.0/",
                ),
            ),
        ),
        item(
            "media_workspace",
            "媒体工作目录",
            workspace_ready,
            "core",
            "由客户端自动创建，无需下载",
        ),
    ]
    if voice_declared:
        dependencies.insert(
            0,
            item(
                "voice_reference",
                "受管林离音色",
                voice_ready,
                voice_install_mode,
                voice_source_summary,
                reason_code=voice_reason,
            ),
        )
    ordinary_ids = {
        "breeze_tts2",
        "latentsync",
        "official_video_assets",
        "ffmpeg",
    }
    if voice_declared:
        ordinary_ids.add("voice_reference")
    ordinary_missing = [
        item["id"]
        for item in dependencies
        if item["id"] in ordinary_ids and item["state"] != "ready"
    ]
    ordinary_ready = not ordinary_missing
    music_ready = bool(
        ordinary_ready
        and minimax_ready
        and roformer_ready
        and music_assets_ready
        and musical_reply_configured(env, performance_video_path=performance_video_path)
    )
    return {
        "ready": music_ready,
        "music_ready": music_ready,
        "ordinary_missing_dependencies": ordinary_missing,
        "dependencies": dependencies,
    }


class MiniMaxMusic3Worker:
    """One-shot local MiniMax Music 3 process adapter."""

    def __init__(
        self,
        *,
        python_path: Path,
        worker_path: Path,
        comfy_root: Path,
        timeout_seconds: float = _MINIMAX_WORKER_TIMEOUT_SECONDS,
    ) -> None:
        self.python_path = Path(python_path)
        self.worker_path = Path(worker_path)
        self.comfy_root = Path(comfy_root)
        self.timeout_seconds = timeout_seconds

    def generate(
        self,
        content: str,
        reply_text: str,
        destination: Path,
        *,
        duration_seconds: int,
        lyrics: str = "",
        caption: str = "",
    ) -> dict[str, object]:
        duration_seconds = normalize_music_duration(duration_seconds)
        if not self.python_path.is_file() or not self.worker_path.is_file():
            raise MusicReplyError("MINIMAX_MUSIC3_UNAVAILABLE")
        if not (self.comfy_root / "main.py").is_file():
            raise MusicReplyError("MINIMAX_MUSIC3_UNAVAILABLE")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="olivia-minimax-", dir=destination.parent) as temporary:
            request_path = Path(temporary) / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "content": str(content),
                        "reply_text": str(reply_text),
                        "max_duration": duration_seconds,
                        "lyrics": str(lyrics),
                        "caption": str(caption),
                        "seed": 200717,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _run(
                [
                    str(self.python_path),
                    str(self.worker_path),
                    "--comfy-root",
                    str(self.comfy_root),
                    "--request-json",
                    str(request_path),
                    "--output",
                    str(destination),
                ],
                "MINIMAX_MUSIC3_FAILED",
                timeout=self.timeout_seconds,
                cleanup_path=destination,
            )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise MusicReplyError("MINIMAX_MUSIC3_OUTPUT_MISSING")
        return {
            "audio_model": "MiniMax-Music-3",
            "requested_duration_seconds": duration_seconds,
            "lyrics_source": "letter_and_reply",
        }


class AceStepClient:
    """Small client for the maintained ACE-Step 1.5 loopback API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise MusicReplyError("ACESTEP_ENDPOINT_NOT_LOOPBACK")
        if parsed.port is None:
            raise MusicReplyError("ACESTEP_ENDPOINT_INVALID")
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        failure = None
        try:
            # The first local request may download and initialize several GB
            # of model weights before the API flushes its response body.
            with urlopen(request, timeout=min(600.0, self.timeout_seconds)) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            failure = _provider_exception_failure("ACESTEP_UNAVAILABLE", exc)
        if failure is not None:
            raise failure
        if not isinstance(result, dict) or result.get("code") != 200:
            raise MusicReplyError("ACESTEP_PROTOCOL_ERROR")
        return result

    def _download(self, file_url: str, destination: Path) -> None:
        resolved = urljoin(self.base_url, file_url)
        parsed = urlsplit(resolved)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise MusicReplyError("ACESTEP_AUDIO_URL_NOT_LOOPBACK")
        failure = None
        try:
            with urlopen(resolved, timeout=min(60.0, self.timeout_seconds)) as response:
                payload = response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            failure = _provider_exception_failure("ACESTEP_AUDIO_UNAVAILABLE", exc)
        if failure is not None:
            raise failure
        if not payload:
            raise MusicReplyError("ACESTEP_AUDIO_EMPTY")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    def generate(
        self,
        content: str,
        reply_text: str,
        destination: Path,
        *,
        duration_seconds: int,
    ) -> dict[str, object]:
        duration_seconds = normalize_music_duration(duration_seconds)
        letter_line = " ".join(str(content).strip().split())[:180]
        reply_line = " ".join(str(reply_text).strip().split())[:180]
        lyrics = (
            "[Verse 1]\n"
            f"{letter_line}\n"
            f"{reply_line}\n\n"
            "[Chorus]\n"
            "我把这一封回信唱给你\n"
            "愿新家的灯一直温暖明亮\n"
            "平常的今天也值得被珍藏\n\n"
            "[Outro]\n"
            "谢谢你让我在这里有了新的家"
        )
        released = self._post("/release_task", {
            "sample_mode": False,
            "prompt": (
                "Warm restrained Mandarin piano ballad, intimate female solo vocal, "
                "gentle acoustic piano, subtle strings, no rap, no male vocal, emotional but natural."
            ),
            "lyrics": lyrics,
            "thinking": True,
            "use_format": False,
            "use_cot_caption": False,
            "use_cot_language": False,
            "model": "acestep-v15-turbo",
            "lm_model_path": "acestep-5Hz-lm-0.6B",
            "lm_backend": "pt",
            "audio_duration": duration_seconds,
            "bpm": 78,
            "key_scale": "F Major",
            "time_signature": "4",
            "audio_format": "wav",
            "batch_size": 1,
            "vocal_language": "zh",
            "inference_steps": 8,
        })
        data = released.get("data")
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise MusicReplyError("ACESTEP_TASK_INVALID")

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            queried = self._post("/query_result", {"task_id_list": [task_id]})
            rows = queried.get("data")
            row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
            if row is None:
                raise MusicReplyError("ACESTEP_RESULT_INVALID")
            status = row.get("status")
            if status == 2:
                raise MusicReplyError("ACESTEP_GENERATION_FAILED")
            if status == 1:
                failure = None
                try:
                    items = json.loads(str(row.get("result", "")))
                except json.JSONDecodeError as exc:
                    failure = _provider_exception_failure("ACESTEP_RESULT_INVALID", exc)
                if failure is not None:
                    raise failure
                item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
                if item is None or not isinstance(item.get("file"), str):
                    raise MusicReplyError("ACESTEP_RESULT_INVALID")
                self._download(item["file"], destination)
                metas = item.get("metas") if isinstance(item.get("metas"), dict) else {}
                return {
                    "lyrics": str(item.get("lyrics", "")),
                    "prompt": str(item.get("prompt", "")),
                    "duration_seconds": float(metas.get("duration", 0.0) or 0.0),
                    "audio_model": "ACE-Step-1.5",
                }
            time.sleep(self.poll_interval_seconds)
        raise MusicReplyError("ACESTEP_TIMEOUT")


def _ffmpeg(ffmpeg_path: Path | None = None) -> str:
    try:
        if ffmpeg_path is not None:
            if not ffmpeg_path.is_absolute() or not ffmpeg_path.is_file():
                raise LatentSyncReplyError("LATENTSYNC_FFMPEG_UNAVAILABLE")
            return str(ffmpeg_path)
        return str(resolve_ffmpeg_executable())
    except LatentSyncReplyError as exc:
        raise MusicReplyError("FFMPEG_UNAVAILABLE") from exc


def _run(
    command: list[str],
    error_code: str,
    *,
    timeout: float = 900.0,
    env: dict[str, str] | None = None,
    cleanup_path: Path | None = None,
) -> None:
    try:
        result = run_managed_process(command, timeout_seconds=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure_category = "TimeoutExpired" if isinstance(exc, subprocess.TimeoutExpired) else getattr(exc, "stderr", None) or type(exc).__name__
        diagnostic = _provider_failure_diagnostic(returncode="unavailable", stderr=failure_category)
        _persist_provider_failure(error_code, diagnostic, env)
        failure = MusicReplyError(error_code, diagnostic=diagnostic)
    else:
        failure = None
    if failure is None and result.returncode != 0:
        diagnostic = _provider_failure_diagnostic(returncode=result.returncode, stderr=result.stderr)
        _persist_provider_failure(error_code, diagnostic, env)
        failure = MusicReplyError(error_code, diagnostic=diagnostic)
    if failure is not None:
        if cleanup_path is not None:
            try: cleanup_path.unlink(missing_ok=True)
            except OSError: pass
        raise failure


def prepare_official_spoken_base(
    reference_path: Path,
    destination: Path,
    *,
    ffmpeg_path: Path | None = None,
) -> Path:
    """Create the verified 0-35s speaking-performance base for musical replies."""

    reference_path = Path(reference_path)
    destination = Path(destination)
    if not reference_path.is_file():
        raise MusicReplyError("MUSIC_REPLY_SPOKEN_REFERENCE_UNAVAILABLE")
    spoken_gate = {"required_streams": ("0:v:0",), "ffmpeg_path": ffmpeg_path,
                   "minimum_duration_seconds": 30.0, "forbidden_streams": ("0:a:0",)}
    if _completed_stage(destination, **spoken_gate):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.stem}.partial{destination.suffix}")
    partial.unlink(missing_ok=True)
    _run(
        [
            _ffmpeg(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "0",
            "-i",
            str(reference_path),
            "-t",
            "35",
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(partial),
        ],
        "MUSIC_REPLY_SPOKEN_REFERENCE_FAILED",
        timeout=900.0,
    )
    if not _completed_stage(partial, **spoken_gate):
        raise MusicReplyError("MUSIC_REPLY_SPOKEN_REFERENCE_FAILED")
    partial.replace(destination)
    return destination


def separate_vocals(
    song_path: Path,
    vocals_path: Path,
    *,
    executable: Path | None,
    model_path: Path | None,
    config_path: Path | None,
    environment: Mapping[str, str],
    ffmpeg_path: Path | None = None,
) -> None:
    if any(
        value is None or not value.is_file()
        for value in (executable, model_path, config_path)
    ):
        raise MusicReplyError("ROFORMER_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="olivia-roformer-", dir=vocals_path.parent) as temporary:
        root = Path(temporary)
        inputs = root / "inputs"
        outputs = root / "outputs"
        inputs.mkdir()
        outputs.mkdir()
        source = inputs / "song.wav"
        _run(
            [
                _ffmpeg(ffmpeg_path),
                "-y",
                "-i",
                str(song_path),
                "-ar",
                "44100",
                "-ac",
                "2",
                str(source),
            ],
            "ROFORMER_INPUT_CONVERSION_FAILED",
        )
        command = [str(executable)]
        configured_python = environment.get("OLIVIA_ROFORMER_PYTHON")
        if configured_python and Path(str(configured_python)).resolve() == executable.resolve():
            command.extend(["-m", "mel_band_roformer.inference"])
        command.extend(
            [
                "--input_folder",
                str(inputs),
                "--store_dir",
                str(outputs),
                "--model_path",
                str(model_path),
                "--config_path",
                str(config_path),
            ]
        )
        _run(
            command,
            "ROFORMER_FAILED",
            env={
                **environment,
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            },
        )
        candidates = sorted(outputs.rglob("*vocals*.wav"))
        if not candidates:
            raise MusicReplyError("ROFORMER_OUTPUT_MISSING")
        if not _valid_wave_audio(candidates[0]):
            raise MusicReplyError("ROFORMER_OUTPUT_INVALID")
        vocals_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidates[0], vocals_path)


def _valid_wave_audio(path: Path, *, minimum_duration_seconds: float = 0.1) -> bool:
    duration = _media_duration_seconds(path, required_streams=("0:a:0",))
    return duration is not None and duration >= minimum_duration_seconds


def render_full_face_performance(
    performance_video_path: Path,
    vocals_path: Path,
    full_song_path: Path,
    output_path: Path,
    *,
    latentsync_python_path: Path | None,
    latentsync_root: Path | None,
    ffmpeg_path: Path | None = None,
    provider_cache_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if latentsync_python_path is None or latentsync_root is None:
        raise MusicReplyError("LATENTSYNC_INPUT_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="olivia-music-face-", dir=output_path.parent) as temporary:
        raw_video = Path(temporary) / "latentsync-vocals.mp4"
        failure = None
        try:
            metadata = render_latentsync_video(
                performance_video_path,
                vocals_path,
                raw_video,
                python_path=latentsync_python_path,
                latentsync_root=latentsync_root,
                ffmpeg_path=ffmpeg_path,
                provider_cache_root=provider_cache_root,
                environment=environment,
            )
        except LatentSyncReplyError as exc:
            failure = _provider_exception_failure(str(exc), exc, environment)
        if failure is not None:
            raise failure
        _run(
            [
                _ffmpeg(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(raw_video), "-i", str(full_song_path),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-movflags", "+faststart", str(output_path),
            ],
            "MUSIC_REPLY_AUDIO_MUX_FAILED",
            timeout=900.0,
            cleanup_path=output_path,
        )
    return {**metadata, "face_model": "LatentSync-1.5", "mouth_refiner": "native_face_paste"}


def _target_frame_count(video_path: Path, fps: int = 25) -> int:
    try:
        import imageio_ffmpeg

        _source_frames, duration = imageio_ffmpeg.count_frames_and_secs(str(video_path))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise MusicReplyError("MUSIC_REPLY_DURATION_UNAVAILABLE") from exc
    frames = round(float(duration) * fps)
    if frames <= 0:
        raise MusicReplyError("MUSIC_REPLY_DURATION_UNAVAILABLE")
    return frames


def concat_videos(
    normal_video_path: Path,
    song_video_path: Path,
    output_path: Path,
    *,
    transition_video_path: Path | None = None,
    transition_start_seconds: float = 35.0,
    transition_end_seconds: float = 43.0,
    end_fade_seconds: float = 2.0,
    ffmpeg_path: Path | None = None,
) -> None:
    """Join speech and performance, optionally preserving the reference turn/transition.

    The transition picture is taken from a bundled reference, but its audio is
    deliberately replaced with silence so old speech or music cannot leak into a
    newly generated reply.
    """

    if transition_video_path is not None:
        transition_video_path = Path(transition_video_path)
        if not transition_video_path.is_file():
            raise MusicReplyError("MUSIC_REPLY_TRANSITION_UNAVAILABLE")
        if transition_start_seconds < 0 or transition_end_seconds <= transition_start_seconds:
            raise MusicReplyError("MUSIC_REPLY_TRANSITION_RANGE_INVALID")
    if end_fade_seconds <= 0:
        raise MusicReplyError("MUSIC_REPLY_FADE_DURATION_INVALID")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="olivia-concat-", dir=output_path.parent) as temporary:
        root = Path(temporary)
        target_frames = _target_frame_count(normal_video_path) + _target_frame_count(song_video_path)
        video_filter = (
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,"
            "fps=25,setsar=1,setpts=PTS-STARTPTS"
        )
        audio_filter = (
            "aresample=44100,"
            "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            "asetpts=PTS-STARTPTS"
        )
        inputs = ["-i", str(normal_video_path), "-i", str(song_video_path)]
        filters = [
            f"[0:v]{video_filter}[normal_v]",
            f"[0:a]{audio_filter}[normal_a]",
            f"[1:v]{video_filter}[song_v]",
            f"[1:a]{audio_filter}[song_a]",
        ]
        if transition_video_path is None:
            filters.append(
                "[normal_v][normal_a][song_v][song_a]"
                "concat=n=2:v=1:a=1[joined_v][joined_a]"
            )
        else:
            duration = transition_end_seconds - transition_start_seconds
            transition_frames = round(duration * 25)
            if transition_frames <= 0:
                raise MusicReplyError("MUSIC_REPLY_TRANSITION_RANGE_INVALID")
            target_frames += transition_frames
            inputs.extend(["-i", str(transition_video_path)])
            filters.extend([
                (
                    f"[2:v]trim=start={transition_start_seconds:.6f}:"
                    f"end={transition_end_seconds:.6f},{video_filter}[transition_v]"
                ),
                (
                    "anullsrc=r=44100:cl=stereo,"
                    f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[transition_a]"
                ),
                (
                    "[normal_v][normal_a][transition_v][transition_a][song_v][song_a]"
                    "concat=n=3:v=1:a=1[joined_v][joined_a]"
                ),
            ])
        target_duration = target_frames / 25
        fade_duration = min(float(end_fade_seconds), target_duration)
        fade_start = target_duration - fade_duration
        filters.extend(
            [
                (
                    f"[joined_v]fade=t=out:st={fade_start:.6f}:"
                    f"d={fade_duration:.6f}[final_v]"
                ),
                (
                    f"[joined_a]afade=t=out:st={fade_start:.6f}:"
                    f"d={fade_duration:.6f}[final_a]"
                ),
            ]
        )
        assembled = root / "assembled-lossless.mkv"
        _run(
            [
                _ffmpeg(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
                *inputs,
                "-filter_complex", ";".join(filters),
                "-map", "[final_v]", "-map", "[final_a]",
                "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
                "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(assembled),
            ],
            "MUSIC_REPLY_ASSEMBLY_FAILED",
            timeout=900.0,
        )
        result = root / "complete.mp4"
        _run(
            [
                _ffmpeg(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(assembled),
                "-vf", "tpad=stop_mode=clone:stop_duration=1",
                "-af", (
                    "apad,"
                    f"atrim=duration={target_duration:.6f},asetpts=PTS-STARTPTS"
                ),
                "-frames:v", str(target_frames),
                "-t", f"{target_duration:.6f}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-r", "25",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart", str(result),
            ],
            "MUSIC_REPLY_CONCAT_FAILED",
            timeout=900.0,
        )
        result.replace(output_path)


def _media_duration_seconds(path: Path, *, required_streams: tuple[str, ...], ffmpeg_path: Path | None = None,
                            forbidden_streams: tuple[str, ...] = ()) -> float | None:
    if not path.is_file():
        return None
    if path.suffix.casefold() == ".wav":
        if required_streams != ("0:a:0",):
            return None
        try:
            with wave.open(str(path), "rb") as stream:
                frame_rate, frame_count = stream.getframerate(), stream.getnframes()
                expected_size = frame_count * stream.getnchannels() * stream.getsampwidth()
                payload = stream.readframes(frame_count)
            if frame_rate <= 0 or expected_size <= 0 or len(payload) < expected_size: return None
            return frame_count / frame_rate
        except (EOFError, OSError, wave.Error): return None
    def decode(stream: str) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return subprocess.run([
                _ffmpeg(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror",
                "-i", str(path), "-map", stream, "-progress", "pipe:1", "-nostats", "-f", "null", os.devnull,
            ], capture_output=True, check=False, timeout=_MEDIA_VALIDATION_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (MusicReplyError, OSError, subprocess.TimeoutExpired): return None

    durations: list[float] = []
    for stream in required_streams:
        result = decode(stream)
        if result is None or result.returncode != 0: return None
        stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else str(result.stdout)
        timestamps = [line.partition("=")[2].strip() for line in stdout.splitlines() if line.startswith("out_time=")]
        try:
            hours, minutes, seconds = timestamps[-1].split(":", 2)
            durations.append(int(hours) * 3600 + int(minutes) * 60 + float(seconds))
        except (IndexError, TypeError, ValueError): return None
    for stream in forbidden_streams:
        result = decode(stream)
        stderr = result.stderr.decode("utf-8", errors="replace").casefold() if result and isinstance(result.stderr, bytes) else str(result.stderr if result else "").casefold()
        if result is None or result.returncode == 0 or "matches no streams" not in stderr:
            return None
    return min(durations, default=None)


def _completed_stage(path: Path, *, required_streams: tuple[str, ...], ffmpeg_path: Path | None = None,
                     minimum_duration_seconds: float = 0.1,
                     forbidden_streams: tuple[str, ...] = ()) -> bool:
    try:
        duration = _media_duration_seconds(path, required_streams=required_streams, ffmpeg_path=ffmpeg_path, forbidden_streams=forbidden_streams)
        return bool(path.stat().st_size > 0 and duration is not None and duration >= minimum_duration_seconds)
    except OSError:
        return False


def _file_fingerprint(path: Path | None) -> dict[str, object]:
    """Return a content-bound fingerprint without retaining local path names."""

    if path is None:
        return {"status": "not_configured"}
    path = Path(path)
    try:
        if not path.is_file():
            return {"status": "missing"}
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "status": "present",
            "size": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    except OSError:
        return {"status": "unavailable"}


def _build_music_stage_manifest(
    content: str,
    reply_text: str,
    song_plan: object,
    duration_seconds: int,
    *,
    official_reply_reference_path: Path,
    spoken_action_base_path: Path | None,
    transition_reference: Path,
    performance_video_path: Path,
    tts_config_path: Path,
    visual_config_path: Path,
    worker_path: Path,
    minimax_worker_path: Path,
    minimax_root: Path,
    provider_paths: MusicProviderPathSnapshot,
    voice_performance_plan: VoicePerformancePlan | None,
) -> dict[str, object]:
    """Bind resumable stages to canonical text, inputs, and provider revisions."""

    return {
        "schema_version": _MUSIC_STAGE_MANIFEST_VERSION,
        "inputs": {
            "letter_sha256": hashlib.sha256(str(content).encode("utf-8")).hexdigest(),
            "canonical_reply_sha256": hashlib.sha256(
                str(reply_text).encode("utf-8")
            ).hexdigest(),
            "lyrics_sha256": hashlib.sha256(
                str(song_plan.lyrics).encode("utf-8")
            ).hexdigest(),
            "caption_sha256": hashlib.sha256(
                str(song_plan.caption).encode("utf-8")
            ).hexdigest(),
            "duration_seconds": duration_seconds,
            "voice_performance_sha256": hashlib.sha256(
                json.dumps(
                    voice_performance_plan.to_dict() if voice_performance_plan else None,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        },
        "assets": {
            "official_reply_reference": _file_fingerprint(official_reply_reference_path),
            "spoken_action_base": _file_fingerprint(spoken_action_base_path),
            "official_transition_reference": _file_fingerprint(transition_reference),
            "performance_video": _file_fingerprint(performance_video_path),
            "tts_config": _file_fingerprint(tts_config_path),
            "visual_config": _file_fingerprint(visual_config_path),
            "visual_worker": _file_fingerprint(worker_path),
        },
        "providers": {
            "music": {
                "name": "MiniMax-Music-3",
                "python": _file_fingerprint(
                    provider_paths.minimax_python
                ),
                "worker": _file_fingerprint(minimax_worker_path),
                "entry": _file_fingerprint(minimax_root / "main.py"),
                "node_code": _file_fingerprint(
                    minimax_root / "comfy_extras" / "nodes_minimax_music.py"
                ),
                "models": {
                    "unet": _file_fingerprint(
                        minimax_root
                        / "models"
                        / "diffusion_models"
                        / "minimax_music3_dit_int8_convrot.safetensors"
                    ),
                    "text_encoder": _file_fingerprint(
                        minimax_root
                        / "models"
                        / "text_encoders"
                        / "minimax_music3_text_encoder_pruned_int8_convrot.safetensors"
                    ),
                    "vae": _file_fingerprint(
                        minimax_root
                        / "models"
                        / "vae"
                        / "minimax_music3_dav.safetensors"
                    ),
                },
            },
            "vocal_separator": {
                "name": "MelBand-RoFormer",
                "executable": _file_fingerprint(provider_paths.roformer_executable),
                "model": _file_fingerprint(provider_paths.roformer_model),
                "config": _file_fingerprint(provider_paths.roformer_config),
            },
            "face_sync": {
                "name": "LatentSync-1.5",
                "python": _file_fingerprint(
                    provider_paths.latentsync_python
                ),
                "inference": _file_fingerprint(
                    provider_paths.latentsync_root / "scripts" / "inference.py"
                    if provider_paths.latentsync_root is not None
                    else None
                ),
                "config": _file_fingerprint(
                    provider_paths.latentsync_root / "configs" / "unet" / "stage2_efficient.yaml"
                    if provider_paths.latentsync_root is not None
                    else None
                ),
                "checkpoint": _file_fingerprint(
                    provider_paths.latentsync_root / "checkpoints" / "latentsync_unet.pt"
                    if provider_paths.latentsync_root is not None
                    else None
                ),
            },
        },
        "artifacts": {},
    }


def _read_compatible_manifest(
    manifest_path: Path,
    expected: dict[str, object],
) -> dict[str, object]:
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**expected, "artifacts": {}}
    if not isinstance(loaded, dict) or any(
        loaded.get(key) != expected.get(key)
        for key in ("schema_version", "inputs", "assets", "providers")
    ):
        artifacts: dict[str, object] = {}
        if isinstance(loaded, dict) and _normal_stage_inputs_match(loaded, expected):
            loaded_artifacts = loaded.get("artifacts")
            normal = (
                loaded_artifacts.get("normal_video")
                if isinstance(loaded_artifacts, dict)
                else None
            )
            if isinstance(normal, dict):
                artifacts["normal_video"] = normal
        return {**expected, "artifacts": artifacts}
    artifacts = loaded.get("artifacts")
    return {**expected, "artifacts": artifacts if isinstance(artifacts, dict) else {}}


def _normal_stage_inputs_match(
    loaded: dict[str, object],
    expected: dict[str, object],
) -> bool:
    """Keep completed speech when only song planning or music providers change."""

    if loaded.get("schema_version") != expected.get("schema_version"):
        return False
    for section, keys in (
        ("inputs", ("canonical_reply_sha256", "voice_performance_sha256")),
        (
            "assets",
            (
                "official_reply_reference",
                "spoken_action_base",
                "tts_config",
                "visual_config",
                "visual_worker",
            ),
        ),
        ("providers", ("face_sync",)),
    ):
        old_values = loaded.get(section)
        new_values = expected.get(section)
        if not isinstance(old_values, dict) or not isinstance(new_values, dict):
            return False
        if any(old_values.get(key) != new_values.get(key) for key in keys):
            return False
    return True


def _write_stage_manifest(manifest_path: Path, manifest: dict[str, object]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    partial = manifest_path.with_name(f"{manifest_path.stem}.partial{manifest_path.suffix}")
    partial.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    partial.replace(manifest_path)


def _stage_reusable(
    manifest: dict[str, object],
    artifact_name: str,
    path: Path,
    *,
    upstream: dict[str, Path] | None = None,
    required_streams: tuple[str, ...],
    ffmpeg_path: Path | None = None,
    minimum_duration_seconds: float = 0.1,
    forbidden_streams: tuple[str, ...] = (),
) -> bool:
    artifacts = manifest.get("artifacts")
    expected = artifacts.get(artifact_name) if isinstance(artifacts, dict) else None
    current = _stage_record(path, upstream)
    return (
        _completed_stage(
            path,
            required_streams=required_streams,
            ffmpeg_path=ffmpeg_path,
            minimum_duration_seconds=minimum_duration_seconds,
            forbidden_streams=forbidden_streams,
        )
        and isinstance(expected, dict)
        and all(expected.get(key) == value for key, value in current.items())
    )


def _stage_record(
    path: Path,
    upstream: dict[str, Path] | None = None,
) -> dict[str, object]:
    return {
        "fingerprint": _file_fingerprint(path),
        "upstream": {
            name: _file_fingerprint(source)
            for name, source in sorted((upstream or {}).items())
        },
    }


def _require_stage(path: Path, artifact_name: str, required_streams: tuple[str, ...],
                   ffmpeg_path: Path | None, minimum_duration_seconds: float,
                   forbidden_streams: tuple[str, ...] = ()) -> None:
    try:
        duration, size = _media_duration_seconds(path, required_streams=required_streams, ffmpeg_path=ffmpeg_path, forbidden_streams=forbidden_streams), path.stat().st_size
    except OSError:
        duration, size = None, 0
    if size <= 0 or duration is None or duration < minimum_duration_seconds:
        observed = "unavailable" if duration is None else f"{duration:.3f}"
        raise MusicReplyError("MUSIC_STAGE_OUTPUT_INVALID", diagnostic=f"stage={artifact_name};observed_seconds={observed};required_seconds={minimum_duration_seconds:.3f}")


def _publish_stage(partial: Path, destination: Path, artifact_name: str, required_streams: tuple[str, ...],
                   ffmpeg_path: Path | None, minimum_duration_seconds: float) -> None:
    try:
        _require_stage(partial, artifact_name, required_streams, ffmpeg_path, minimum_duration_seconds)
        partial.replace(destination)
    finally:
        try: partial.unlink(missing_ok=True)
        except OSError: pass


def _record_stage(
    manifest: dict[str, object],
    manifest_path: Path,
    artifact_name: str,
    path: Path,
    *,
    upstream: dict[str, Path] | None = None,
    required_streams: tuple[str, ...],
    ffmpeg_path: Path | None = None,
    minimum_duration_seconds: float = 0.1,
    forbidden_streams: tuple[str, ...] = (),
) -> None:
    _require_stage(path, artifact_name, required_streams, ffmpeg_path, minimum_duration_seconds, forbidden_streams)
    artifacts = manifest.setdefault("artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}
        manifest["artifacts"] = artifacts
    artifacts[artifact_name] = _stage_record(path, upstream)
    _write_stage_manifest(manifest_path, manifest)


def render_musical_reply(
    content: str,
    reply_text: str,
    output_path: Path,
    *,
    normal_video_path: Path,
    official_reply_reference_path: Path,
    song_video_path: Path,
    tts_config_path: Path,
    visual_config_path: Path,
    worker_path: Path,
    performance_video_path: Path,
    duration_seconds: int,
    spoken_action_base_path: Path | None = None,
    voice_performance_plan: VoicePerformancePlan | None = None,
    gateway: Gateway | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Render the ordinary reply, append an original-view song performance."""

    require_breeze_hardware()
    duration_seconds = normalize_music_duration(duration_seconds)
    provider_paths = _music_provider_path_snapshot(environment)
    transition_reference = Path(official_reply_reference_path)
    if not transition_reference.is_file():
        raise MusicReplyError("MUSIC_REPLY_TRANSITION_UNAVAILABLE")
    minimax_python = provider_paths.minimax_python
    minimax_root = provider_paths.minimax_root
    minimax_worker = provider_paths.minimax_worker
    if any(path is None for path in (minimax_python, minimax_root, minimax_worker)):
        raise MusicReplyError("MINIMAX_MUSIC3_UNAVAILABLE")
    latentsync_python = provider_paths.latentsync_python
    latentsync_root = provider_paths.latentsync_root
    if latentsync_python is None or latentsync_root is None:
        raise MusicReplyError("LATENTSYNC_INPUT_UNAVAILABLE")
    ffmpeg_path = provider_paths.ffmpeg_executable
    if ffmpeg_path is None or not ffmpeg_path.is_file():
        raise MusicReplyError("FFMPEG_UNAVAILABLE")
    provider_cache_root = provider_paths.provider_cache_root
    if provider_cache_root is None or not provider_cache_root.is_absolute():
        raise MusicReplyError("LATENTSYNC_INPUT_UNAVAILABLE")
    try:
        planner_options = {"gateway": gateway} if gateway is not None else {}
        song_plan = plan_song_content(
            content,
            reply_text,
            duration_seconds,
            **planner_options,
        )
    except Exception as exc:
        raise MusicReplyError("SONG_CONTENT_UNAVAILABLE") from exc
    stage_root = output_path.parent / (
        f"{output_path.stem}-music-v2-{duration_seconds}s-stages"
    )
    stage_root.mkdir(parents=True, exist_ok=True)
    manifest_path = stage_root / "manifest.json"
    expected_manifest = _build_music_stage_manifest(
        content,
        reply_text,
        song_plan,
        duration_seconds,
        official_reply_reference_path=official_reply_reference_path,
        spoken_action_base_path=spoken_action_base_path,
        transition_reference=transition_reference,
        performance_video_path=performance_video_path,
        tts_config_path=tts_config_path,
        visual_config_path=visual_config_path,
        worker_path=worker_path,
        minimax_worker_path=minimax_worker,
        minimax_root=minimax_root,
        provider_paths=provider_paths,
        voice_performance_plan=voice_performance_plan,
    )
    try:
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing_manifest = None
    manifest_compatible = isinstance(existing_manifest, dict) and all(
        existing_manifest.get(key) == expected_manifest.get(key)
        for key in ("schema_version", "inputs", "assets", "providers")
    )
    manifest = _read_compatible_manifest(manifest_path, expected_manifest)
    if not manifest_compatible:
        _write_stage_manifest(manifest_path, manifest)

    spoken_base = (
        Path(spoken_action_base_path)
        if spoken_action_base_path is not None
        else stage_root / "official-spoken-000-035s.mp4"
    )
    if spoken_action_base_path is not None and not spoken_base.is_file():
        raise MusicReplyError("MUSIC_REPLY_SPOKEN_REFERENCE_UNAVAILABLE")
    spoken_gate = {"required_streams": ("0:v:0",), "ffmpeg_path": ffmpeg_path,
                   "minimum_duration_seconds": 30.0, "forbidden_streams": ("0:a:0",)}
    normal_gate = {"required_streams": ("0:v:0", "0:a:0"),
                   "ffmpeg_path": ffmpeg_path, "minimum_duration_seconds": 1.0}
    spoken_ready = _completed_stage(spoken_base, **spoken_gate)
    if spoken_action_base_path is not None and not spoken_ready:
        raise MusicReplyError("MUSIC_REPLY_SPOKEN_REFERENCE_FAILED")
    if spoken_ready and _stage_reusable(
        manifest,
        "normal_video",
        normal_video_path,
        upstream={"spoken_base": spoken_base},
        **normal_gate,
    ):
        normal_metadata = {"spoken_stage": "reused"}
        normal_record = manifest.get("artifacts", {}).get("normal_video", {})
        if (
            isinstance(normal_record, dict)
            and normal_record.get("audio_provider") == "breeze_tts2"
        ):
            normal_metadata["audio_provider"] = "breeze_tts2"
    else:
        if (
            spoken_action_base_path is None
            and not _stage_reusable(manifest, "spoken_base", spoken_base, **spoken_gate)
        ):
            spoken_base.unlink(missing_ok=True)
            prepare_official_spoken_base(
                official_reply_reference_path,
                spoken_base,
                ffmpeg_path=ffmpeg_path,
            )
            _record_stage(manifest, manifest_path, "spoken_base", spoken_base, **spoken_gate)
        partial_normal = normal_video_path.with_name(f"{normal_video_path.stem}.partial{normal_video_path.suffix}")
        partial_normal.unlink(missing_ok=True)
        normal_failure = None
        try:
            normal_metadata = render_reply_video(
                reply_text,
                partial_normal,
                tts_config_path=tts_config_path,
                visual_config_path=visual_config_path,
                worker_path=worker_path,
                scene_path=spoken_base,
                latentsync_python_path=latentsync_python,
                latentsync_root=latentsync_root,
                adaptive_delivery=True,
                voice_performance_plan=voice_performance_plan,
                environment=provider_paths.environment,
                ffmpeg_path=ffmpeg_path,
                provider_cache_root=provider_cache_root,
            )
        except ReplyMediaError as exc:
            normal_failure = _provider_exception_failure("MUSIC_REPLY_NORMAL_VIDEO_FAILED", exc, provider_paths.environment)
        if normal_failure is not None:
            try: partial_normal.unlink(missing_ok=True)
            except OSError: pass
            raise normal_failure from None
        _publish_stage(partial_normal, normal_video_path, "normal_video", ("0:v:0", "0:a:0"), ffmpeg_path, 1.0)
        _record_stage(
            manifest,
            manifest_path,
            "normal_video",
            normal_video_path,
            upstream={"spoken_base": spoken_base},
            **normal_gate,
        )
        if normal_metadata.get("audio_provider") == "breeze_tts2":
            normal_record = manifest.get("artifacts", {}).get("normal_video")
            if isinstance(normal_record, dict):
                normal_record["audio_provider"] = "breeze_tts2"
                _write_stage_manifest(manifest_path, manifest)

    song_audio = stage_root / "song.flac"
    vocals = stage_root / "vocals.wav"
    music_stage_minimum = max(1.0, duration_seconds - 5.0)
    audio_gate = {"required_streams": ("0:a:0",), "ffmpeg_path": ffmpeg_path,
                  "minimum_duration_seconds": music_stage_minimum}
    video_gate = {"required_streams": ("0:v:0", "0:a:0"), "ffmpeg_path": ffmpeg_path,
                  "minimum_duration_seconds": music_stage_minimum}
    if _stage_reusable(manifest, "song_audio", song_audio, **audio_gate):
        song_metadata = {
            "audio_model": "MiniMax-Music-3",
            "requested_duration_seconds": duration_seconds,
            "lyrics_source": "letter_and_reply",
            "music_stage": "reused",
        }
    else:
        partial_song = stage_root / "song.partial.flac"
        partial_song.unlink(missing_ok=True)
        song_metadata = MiniMaxMusic3Worker(
            python_path=minimax_python,
            worker_path=minimax_worker,
            comfy_root=minimax_root,
        ).generate(
            content,
            reply_text,
            partial_song,
            duration_seconds=duration_seconds,
            lyrics=song_plan.lyrics,
            caption=song_plan.caption,
        )
        _publish_stage(partial_song, song_audio, "song_audio", ("0:a:0",),
                       ffmpeg_path, music_stage_minimum)
        _record_stage(manifest, manifest_path, "song_audio", song_audio, **audio_gate)

    if not _stage_reusable(
        manifest,
        "vocals",
        vocals,
        upstream={"song_audio": song_audio},
        **audio_gate,
    ):
        partial_vocals = stage_root / "vocals.partial.wav"
        partial_vocals.unlink(missing_ok=True)
        separate_vocals(
            song_audio,
            partial_vocals,
            executable=provider_paths.roformer_executable,
            model_path=provider_paths.roformer_model,
            config_path=provider_paths.roformer_config,
            environment=provider_paths.environment,
            ffmpeg_path=ffmpeg_path,
        )
        _publish_stage(partial_vocals, vocals, "vocals", ("0:a:0",),
                       ffmpeg_path, music_stage_minimum)
        _record_stage(
            manifest,
            manifest_path,
            "vocals",
            vocals,
            upstream={"song_audio": song_audio},
            **audio_gate,
        )

    if _stage_reusable(
        manifest,
        "song_video",
        song_video_path,
        upstream={"song_audio": song_audio, "vocals": vocals},
        **video_gate,
    ):
        face_metadata = {"performance_stage": "reused"}
    else:
        partial_video = song_video_path.with_name(
            f"{song_video_path.stem}.partial{song_video_path.suffix}"
        )
        partial_video.unlink(missing_ok=True)
        face_metadata = render_full_face_performance(
            performance_video_path,
            vocals,
            song_audio,
            partial_video,
            latentsync_python_path=provider_paths.latentsync_python,
            latentsync_root=provider_paths.latentsync_root,
            ffmpeg_path=ffmpeg_path,
            provider_cache_root=provider_cache_root,
            environment=provider_paths.environment,
        )
        _publish_stage(partial_video, song_video_path, "song_video",
                       ("0:v:0", "0:a:0"), ffmpeg_path, music_stage_minimum)
        _record_stage(
            manifest,
            manifest_path,
            "song_video",
            song_video_path,
            upstream={"song_audio": song_audio, "vocals": vocals},
            **video_gate,
        )

    partial_output = output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")
    partial_output.unlink(missing_ok=True)
    concat_videos(
        normal_video_path,
        song_video_path,
        partial_output,
        transition_video_path=transition_reference,
        ffmpeg_path=ffmpeg_path,
    )
    final_stage_minimum = music_stage_minimum + 8.0 + float(normal_gate["minimum_duration_seconds"])
    _publish_stage(partial_output, output_path, "final_output",
                   ("0:v:0", "0:a:0"), ffmpeg_path, final_stage_minimum)
    _record_stage(
        manifest,
        manifest_path,
        "final_output",
        output_path,
        upstream={"normal_video": normal_video_path, "song_video": song_video_path},
        required_streams=("0:v:0", "0:a:0"), ffmpeg_path=ffmpeg_path,
        minimum_duration_seconds=final_stage_minimum,
    )
    return {
        **normal_metadata,
        **song_metadata,
        **face_metadata,
        "song_emotion": song_plan.emotion,
        "lyrics_sha256": hashlib.sha256(song_plan.lyrics.encode("utf-8")).hexdigest(),
        "caption_sha256": hashlib.sha256(song_plan.caption.encode("utf-8")).hexdigest(),
        "song_title": "回信里的歌",
        "reply_structure": (
            "normal_video_then_official_transition_then_song_video"
        ),
        "transition_duration_seconds": 8.0,
    }
