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
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from latentsync_reply import LatentSyncReplyError, render_latentsync_video
from music_duration import MUSIC_DURATION_OPTIONS, normalize_music_duration as _normalize_music_duration
from reply_media import render_reply_video
from song_content import plan_song_content


_MINIMAX_WORKER_TIMEOUT_SECONDS = 7500.0


class MusicReplyError(RuntimeError):
    """Stable product error raised when a song stage cannot complete."""


def normalize_music_duration(value: object) -> int:
    """Return one of the two product durations; intermediate values are invalid."""

    try:
        return _normalize_music_duration(value)
    except ValueError:
        raise MusicReplyError("MUSIC_DURATION_INVALID") from None


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
    configured = os.environ.get("OLIVIA_FFMPEG_EXE", "").strip()
    if configured and Path(configured).is_file():
        return configured
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
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
    executable = os.environ.get("OLIVIA_ROFORMER_EXE", "").strip()
    model_path = os.environ.get("OLIVIA_ROFORMER_MODEL_PATH", "").strip()
    config_path = os.environ.get("OLIVIA_ROFORMER_CONFIG_PATH", "").strip()
    if any(
        not value or not Path(value).is_file()
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
                executable,
                "--input_folder",
                str(inputs),
                "--store_dir",
                str(outputs),
                "--model_path",
                model_path,
                "--config_path",
                config_path,
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
    with tempfile.TemporaryDirectory(prefix="olivia-music-face-", dir=output_path.parent) as temporary:
        raw_video = Path(temporary) / "latentsync-vocals.mp4"
        try:
            metadata = render_latentsync_video(
                performance_video_path,
                vocals_path,
                raw_video,
                python_path=Path(os.environ.get("OLIVIA_LATENTSYNC_PYTHON", "")),
                latentsync_root=Path(os.environ.get("OLIVIA_LATENTSYNC_ROOT", "")),
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


def _official_transition_reference() -> Path | None:
    configured = os.environ.get("OLIVIA_OFFICIAL_REPLY_REFERENCE", "").strip()
    if not configured:
        return None
    reference = Path(configured)
    if not reference.is_file():
        raise MusicReplyError("MUSIC_REPLY_TRANSITION_UNAVAILABLE")
    return reference


def _completed_stage(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


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
) -> dict[str, object]:
    """Render the ordinary reply, append an original-view song performance."""

    duration_seconds = normalize_music_duration(duration_seconds)
    minimax_python = os.environ.get("OLIVIA_MINIMAX_COMFY_PYTHON", "").strip()
    minimax_root = os.environ.get("OLIVIA_MINIMAX_COMFY_ROOT", "").strip()
    minimax_worker = os.environ.get("OLIVIA_MINIMAX_WORKER", "").strip()
    use_minimax = bool(minimax_python and minimax_root and minimax_worker)
    if not use_minimax:
        raise MusicReplyError("MINIMAX_MUSIC3_UNAVAILABLE")
    try:
        song_plan = plan_song_content(content, reply_text, duration_seconds)
    except Exception as exc:
        raise MusicReplyError("SONG_CONTENT_UNAVAILABLE") from exc
    stage_root = output_path.parent / (
        f"{output_path.stem}-music-v2-{duration_seconds}s-stages"
    )
    stage_root.mkdir(parents=True, exist_ok=True)
    if _completed_stage(normal_video_path):
        normal_metadata = {"spoken_stage": "reused"}
    else:
        spoken_base = prepare_official_spoken_base(
            official_reply_reference_path,
            stage_root / "official-spoken-000-035s.mp4",
        )
        normal_metadata = render_reply_video(
            reply_text,
            normal_video_path,
            tts_config_path=tts_config_path,
            visual_config_path=visual_config_path,
            worker_path=worker_path,
            scene_path=spoken_base,
            latentsync_python_path=Path(os.environ.get("OLIVIA_LATENTSYNC_PYTHON", "")),
            latentsync_root=Path(os.environ.get("OLIVIA_LATENTSYNC_ROOT", "")),
            adaptive_delivery=True,
        )

    song_audio = stage_root / "song.flac"
    vocals = stage_root / "vocals.wav"
    if _completed_stage(song_audio):
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
            python_path=Path(minimax_python),
            worker_path=Path(minimax_worker),
            comfy_root=Path(minimax_root),
        ).generate(
            content,
            reply_text,
            partial_song,
            duration_seconds=duration_seconds,
            lyrics=song_plan.lyrics,
            caption=song_plan.caption,
        )
        partial_song.replace(song_audio)

    if not _completed_stage(vocals):
        partial_vocals = stage_root / "vocals.partial.wav"
        partial_vocals.unlink(missing_ok=True)
        separate_vocals(song_audio, partial_vocals)
        partial_vocals.replace(vocals)

    if _completed_stage(song_video_path):
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

    transition_reference = _official_transition_reference()
    if transition_reference is None:
        concat_videos(normal_video_path, song_video_path, output_path)
    else:
        concat_videos(
            normal_video_path,
            song_video_path,
            output_path,
            transition_video_path=transition_reference,
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
            if transition_reference is not None
            else "normal_video_then_song_video"
        ),
        "transition_duration_seconds": 8.0 if transition_reference is not None else 0.0,
    }
