"""Assemble a normal video reply followed by a generated song performance."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from latentsync_reply import (
    LatentSyncReplyError,
    render_latentsync_video,
    resolve_ffmpeg_executable,
)
from media_paths import configured_media_path
from music_duration import MUSIC_DURATION_OPTIONS, normalize_music_duration as _normalize_music_duration
from reply_media import (
    ReplyMediaError,
    assemble_complete_video_delivery,
    render_reply_video,
)
from song_content import plan_song_content
from voice_direction import VoicePerformancePlan


_MINIMAX_WORKER_TIMEOUT_SECONDS = 7500.0
_MUSIC_STAGE_MANIFEST_VERSION = 2


class MusicReplyError(RuntimeError):
    """Stable product error raised when a song stage cannot complete."""


def _configured_provider_path(name: str) -> Path | None:
    return configured_media_path(os.environ, name)


def normalize_music_duration(value: object) -> int:
    """Return one of the two product durations; intermediate values are invalid."""

    try:
        return _normalize_music_duration(value)
    except ValueError:
        raise MusicReplyError("MUSIC_DURATION_INVALID") from None


def speaking_scene_candidates(env: Mapping[str, str]) -> tuple[Path, ...]:
    """Return configured speaking-scene candidates; legacy singleton is a fallback."""

    raw = str(env.get("OLIVIA_SPOKEN_SCENE_CANDIDATES", ""))
    values = raw.split(os.pathsep) if raw else [str(env.get("OLIVIA_OFFICIAL_REPLY_REFERENCE", ""))]
    result: list[Path] = []
    for value in values:
        candidate_env = dict(env)
        candidate_env["_OLIVIA_SCENE_CANDIDATE"] = value
        candidate = configured_media_path(candidate_env, "_OLIVIA_SCENE_CANDIDATE")
        if candidate is not None and candidate not in result:
            result.append(candidate)
    return tuple(result)


def select_speaking_scene(
    candidates: tuple[Path, ...], *, expression: str | None = None
) -> Path | None:
    """Stable default seam; future expression/lip-sync selection can replace this."""

    del expression
    return candidates[0] if candidates else None


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
    speaking_scene = select_speaking_scene(speaking_scene_candidates(env))
    delivery_paths = (
        configured_path("OLIVIA_TTS_CONFIG"),
        configured_path("OLIVIA_VISUAL_CONFIG"),
        configured_path("OLIVIA_LIVETALKING_WORKER"),
        configured_path("OLIVIA_LOCAL_DATA_ROOT"),
    )
    if minimax_root is None or latentsync_root is None or any(
        path is None for path in delivery_paths
    ):
        return False
    try:
        assemble_complete_video_delivery(
            delivery_paths[0],
            delivery_paths[1],
            delivery_paths[2],
            delivery_paths[3],
            env,
        )
    except ReplyMediaError:
        return False
    configured_files = (
        configured_path("OLIVIA_ROFORMER_EXE"),
        configured_path("OLIVIA_ROFORMER_MODEL_PATH"),
        configured_path("OLIVIA_ROFORMER_CONFIG_PATH"),
        configured_path("OLIVIA_MINIMAX_COMFY_PYTHON"),
        configured_path("OLIVIA_MINIMAX_WORKER"),
        configured_path("OLIVIA_LATENTSYNC_PYTHON"),
    )
    if speaking_scene is None or any(path is None for path in configured_files):
        return False
    required = (
        speaking_scene,
        *configured_files[:6],
        minimax_root / "main.py",
        minimax_root / "comfy_extras" / "nodes_minimax_music.py",
        minimax_root / "models" / "unet" / "minimax_music3_dit_int8_convrot.safetensors",
        minimax_root / "models" / "clip" / "minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
        minimax_root / "models" / "vae" / "minimax_music3_dav.safetensors",
        latentsync_root / "scripts" / "inference.py",
        latentsync_root / "configs" / "unet" / "stage2_efficient.yaml",
        latentsync_root / "checkpoints" / "latentsync_unet.pt",
    )
    return bool(
        performance_video_path is not None
        and performance_video_path.is_file()
        and minimax_root.is_dir()
        and latentsync_root.is_dir()
        and all(path.is_file() for path in required)
    )


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
        try:
            # The first local request may download and initialize several GB
            # of model weights before the API flushes its response body.
            with urlopen(request, timeout=min(600.0, self.timeout_seconds)) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            raise MusicReplyError("ACESTEP_UNAVAILABLE") from exc
        if not isinstance(result, dict) or result.get("code") != 200:
            raise MusicReplyError("ACESTEP_PROTOCOL_ERROR")
        return result

    def _download(self, file_url: str, destination: Path) -> None:
        resolved = urljoin(self.base_url, file_url)
        parsed = urlsplit(resolved)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise MusicReplyError("ACESTEP_AUDIO_URL_NOT_LOOPBACK")
        try:
            with urlopen(resolved, timeout=min(60.0, self.timeout_seconds)) as response:
                payload = response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            raise MusicReplyError("ACESTEP_AUDIO_UNAVAILABLE") from exc
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
                try:
                    items = json.loads(str(row.get("result", "")))
                except json.JSONDecodeError as exc:
                    raise MusicReplyError("ACESTEP_RESULT_INVALID") from exc
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


def _ffmpeg() -> str:
    try:
        return str(resolve_ffmpeg_executable())
    except LatentSyncReplyError as exc:
        raise MusicReplyError("FFMPEG_UNAVAILABLE") from exc


def _run(
    command: list[str],
    error_code: str,
    *,
    timeout: float = 900.0,
    env: dict[str, str] | None = None,
) -> None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MusicReplyError(error_code) from exc
    if result.returncode != 0:
        raise MusicReplyError(error_code)


def prepare_official_spoken_base(reference_path: Path, destination: Path) -> Path:
    """Create the verified 0-35s speaking-performance base for musical replies."""

    reference_path = Path(reference_path)
    destination = Path(destination)
    if not reference_path.is_file():
        raise MusicReplyError("MUSIC_REPLY_SPOKEN_REFERENCE_UNAVAILABLE")
    if _completed_stage(destination):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.stem}.partial{destination.suffix}")
    partial.unlink(missing_ok=True)
    _run(
        [
            _ffmpeg(),
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
    if not _completed_stage(partial):
        raise MusicReplyError("MUSIC_REPLY_SPOKEN_REFERENCE_FAILED")
    partial.replace(destination)
    return destination


def separate_vocals(song_path: Path, vocals_path: Path) -> None:
    executable = _configured_provider_path("OLIVIA_ROFORMER_EXE")
    model_path = _configured_provider_path("OLIVIA_ROFORMER_MODEL_PATH")
    config_path = _configured_provider_path("OLIVIA_ROFORMER_CONFIG_PATH")
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
                _ffmpeg(),
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
        _run(
            [
                str(executable),
                "--input_folder",
                str(inputs),
                "--store_dir",
                str(outputs),
                "--model_path",
                str(model_path),
                "--config_path",
                str(config_path),
            ],
            "ROFORMER_FAILED",
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            },
        )
        candidates = sorted(outputs.rglob("*vocals*.wav"))
        if not candidates:
            raise MusicReplyError("ROFORMER_OUTPUT_MISSING")
        vocals_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidates[0], vocals_path)


def render_full_face_performance(
    performance_video_path: Path,
    vocals_path: Path,
    full_song_path: Path,
    output_path: Path,
) -> dict[str, object]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    latentsync_python = _configured_provider_path("OLIVIA_LATENTSYNC_PYTHON")
    latentsync_root = _configured_provider_path("OLIVIA_LATENTSYNC_ROOT")
    if latentsync_python is None or latentsync_root is None:
        raise MusicReplyError("LATENTSYNC_INPUT_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="olivia-music-face-", dir=output_path.parent) as temporary:
        raw_video = Path(temporary) / "latentsync-vocals.mp4"
        try:
            metadata = render_latentsync_video(
                performance_video_path,
                vocals_path,
                raw_video,
                python_path=latentsync_python,
                latentsync_root=latentsync_root,
            )
        except LatentSyncReplyError as exc:
            raise MusicReplyError(str(exc)) from exc
        _run(
            [
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(raw_video), "-i", str(full_song_path),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-movflags", "+faststart", str(output_path),
            ],
            "MUSIC_REPLY_AUDIO_MUX_FAILED",
            timeout=900.0,
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
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
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
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
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


def _official_transition_reference() -> Path:
    reference = select_speaking_scene(speaking_scene_candidates(os.environ))
    if reference is None:
        raise MusicReplyError("MUSIC_REPLY_TRANSITION_UNAVAILABLE")
    if not reference.is_file():
        raise MusicReplyError("MUSIC_REPLY_TRANSITION_UNAVAILABLE")
    return reference


def _completed_stage(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
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


def _optional_path(value: str) -> Path | None:
    value = str(value).strip()
    return Path(value) if value else None


def _build_music_stage_manifest(
    content: str,
    reply_text: str,
    song_plan: object,
    duration_seconds: int,
    *,
    official_reply_reference_path: Path,
    transition_reference: Path,
    performance_video_path: Path,
    tts_config_path: Path,
    visual_config_path: Path,
    worker_path: Path,
    minimax_worker_path: Path,
    minimax_root: Path,
    voice_performance_plan: VoicePerformancePlan | None,
) -> dict[str, object]:
    """Bind resumable stages to canonical text, inputs, and provider revisions."""

    latentsync_root = _configured_provider_path("OLIVIA_LATENTSYNC_ROOT")
    roformer_executable = _configured_provider_path("OLIVIA_ROFORMER_EXE")
    roformer_model = _configured_provider_path("OLIVIA_ROFORMER_MODEL_PATH")
    roformer_config = _configured_provider_path("OLIVIA_ROFORMER_CONFIG_PATH")
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
                    _configured_provider_path("OLIVIA_MINIMAX_COMFY_PYTHON")
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
                        / "unet"
                        / "minimax_music3_dit_int8_convrot.safetensors"
                    ),
                    "text_encoder": _file_fingerprint(
                        minimax_root
                        / "models"
                        / "clip"
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
                "executable": _file_fingerprint(roformer_executable),
                "model": _file_fingerprint(roformer_model),
                "config": _file_fingerprint(roformer_config),
            },
            "face_sync": {
                "name": "LatentSync-1.5",
                "python": _file_fingerprint(
                    _configured_provider_path("OLIVIA_LATENTSYNC_PYTHON")
                ),
                "inference": _file_fingerprint(
                    latentsync_root / "scripts" / "inference.py"
                    if latentsync_root is not None
                    else None
                ),
                "config": _file_fingerprint(
                    latentsync_root / "configs" / "unet" / "stage2_efficient.yaml"
                    if latentsync_root is not None
                    else None
                ),
                "checkpoint": _file_fingerprint(
                    latentsync_root / "checkpoints" / "latentsync_unet.pt"
                    if latentsync_root is not None
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
        return {**expected, "artifacts": {}}
    artifacts = loaded.get("artifacts")
    return {**expected, "artifacts": artifacts if isinstance(artifacts, dict) else {}}


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
) -> bool:
    artifacts = manifest.get("artifacts")
    expected = artifacts.get(artifact_name) if isinstance(artifacts, dict) else None
    return _completed_stage(path) and expected == _stage_record(path, upstream)


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


def _record_stage(
    manifest: dict[str, object],
    manifest_path: Path,
    artifact_name: str,
    path: Path,
    *,
    upstream: dict[str, Path] | None = None,
) -> None:
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
    voice_performance_plan: VoicePerformancePlan | None = None,
) -> dict[str, object]:
    """Render the ordinary reply, append an original-view song performance."""

    duration_seconds = normalize_music_duration(duration_seconds)
    transition_reference = _official_transition_reference()
    minimax_python = _configured_provider_path("OLIVIA_MINIMAX_COMFY_PYTHON")
    minimax_root = _configured_provider_path("OLIVIA_MINIMAX_COMFY_ROOT")
    minimax_worker = _configured_provider_path("OLIVIA_MINIMAX_WORKER")
    if any(path is None for path in (minimax_python, minimax_root, minimax_worker)):
        raise MusicReplyError("MINIMAX_MUSIC3_UNAVAILABLE")
    try:
        song_plan = plan_song_content(content, reply_text, duration_seconds)
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
        transition_reference=transition_reference,
        performance_video_path=performance_video_path,
        tts_config_path=tts_config_path,
        visual_config_path=visual_config_path,
        worker_path=worker_path,
        minimax_worker_path=minimax_worker,
        minimax_root=minimax_root,
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

    spoken_base = stage_root / "official-spoken-000-035s.mp4"
    if _stage_reusable(
        manifest,
        "normal_video",
        normal_video_path,
        upstream={"spoken_base": spoken_base},
    ):
        normal_metadata = {"spoken_stage": "reused"}
    else:
        if not _stage_reusable(manifest, "spoken_base", spoken_base):
            spoken_base.unlink(missing_ok=True)
            prepare_official_spoken_base(official_reply_reference_path, spoken_base)
            _record_stage(manifest, manifest_path, "spoken_base", spoken_base)
        normal_metadata = render_reply_video(
            reply_text,
            normal_video_path,
            tts_config_path=tts_config_path,
            visual_config_path=visual_config_path,
            worker_path=worker_path,
            scene_path=spoken_base,
            latentsync_python_path=_configured_provider_path(
                "OLIVIA_LATENTSYNC_PYTHON"
            ),
            latentsync_root=_configured_provider_path("OLIVIA_LATENTSYNC_ROOT"),
            adaptive_delivery=True,
            voice_performance_plan=voice_performance_plan,
        )
        _record_stage(
            manifest,
            manifest_path,
            "normal_video",
            normal_video_path,
            upstream={"spoken_base": spoken_base},
        )

    song_audio = stage_root / "song.flac"
    vocals = stage_root / "vocals.wav"
    if _stage_reusable(manifest, "song_audio", song_audio):
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
        partial_song.replace(song_audio)
        _record_stage(manifest, manifest_path, "song_audio", song_audio)

    if not _stage_reusable(
        manifest,
        "vocals",
        vocals,
        upstream={"song_audio": song_audio},
    ):
        partial_vocals = stage_root / "vocals.partial.wav"
        partial_vocals.unlink(missing_ok=True)
        separate_vocals(song_audio, partial_vocals)
        partial_vocals.replace(vocals)
        _record_stage(
            manifest,
            manifest_path,
            "vocals",
            vocals,
            upstream={"song_audio": song_audio},
        )

    if _stage_reusable(
        manifest,
        "song_video",
        song_video_path,
        upstream={"song_audio": song_audio, "vocals": vocals},
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
        )
        partial_video.replace(song_video_path)
        _record_stage(
            manifest,
            manifest_path,
            "song_video",
            song_video_path,
            upstream={"song_audio": song_audio, "vocals": vocals},
        )

    concat_videos(
        normal_video_path,
        song_video_path,
        output_path,
        transition_video_path=transition_reference,
    )
    _record_stage(
        manifest,
        manifest_path,
        "final_output",
        output_path,
        upstream={"normal_video": normal_video_path, "song_video": song_video_path},
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
