"""Local singing voice conversion and stem mixing; never fall back to source voice."""
from pathlib import Path
from typing import Mapping
import functools
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import wave


class VoiceConversionError(RuntimeError):
    pass


def convert_singing_voice(vocals: Path, reference: Path, output: Path, *,
                         environment: Mapping[str, str], ffmpeg_path: Path | None) -> None:
    root, python = _runtime(environment)
    if not vocals.is_file() or not reference.is_file():
        raise VoiceConversionError("SOULX_SVC_INPUT_UNAVAILABLE")
    if output.resolve() in (vocals.resolve(), reference.resolve()):
        raise VoiceConversionError("SOULX_SVC_OUTPUT_INVALID")
    output.parent.mkdir(parents=True, exist_ok=True)
    child_env = {**os.environ, **environment}
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"):
        child_env.pop(name, None)
    child_env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONUTF8="1",
                     PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_TELEMETRY="1")
    with tempfile.TemporaryDirectory(prefix="olivia-vc-", dir=output.parent) as folder:
        generated = Path(folder) / "converted.wav"
        command = [str(python), str(_worker_path()), "--runtime", str(root),
                   "--source", str(vocals.resolve()), "--reference", str(reference.resolve()),
                   "--output", str(generated)]
        _run(command, "SOULX_SVC_FAILED", cwd=root, env=child_env, timeout=1800)
        results = [generated] if generated.is_file() else []
        if len(results) != 1 or _duration(results[0], ffmpeg_path) <= 0:
            raise VoiceConversionError("SOULX_SVC_OUTPUT_INVALID")
        if ffmpeg_path is not None:
            if abs(_duration(results[0], ffmpeg_path) - _duration(vocals, ffmpeg_path)) > 0.25:
                raise VoiceConversionError("SOULX_SVC_DURATION_MISMATCH")
        shutil.copyfile(results[0], output)


def _worker_path():
    return Path(__file__).resolve().parents[2] / "tools/soulx_svc_worker.py"


def _runtime(environment):
    root = Path(environment.get("OLIVIA_SOULX_SVC_ROOT", ""))
    python = Path(environment.get("OLIVIA_SOULX_SVC_PYTHON", ""))
    if not root.is_absolute() or not python.is_file() or not (root / "source/soulxsinger/models/soulxsinger_svc.py").is_file():
        raise VoiceConversionError("SOULX_SVC_UNAVAILABLE")
    return root, python


def _run(command, code, **kwargs):
    try:
        result = subprocess.run(command, capture_output=True, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                **{"timeout": 180, **kwargs})
    except (OSError, subprocess.TimeoutExpired):
        raise VoiceConversionError(code) from None
    if result.returncode:
        raise VoiceConversionError(code)
    return result.stdout


def _duration(path, ffmpeg_path):
    if ffmpeg_path is None:
        try:
            with wave.open(str(path)) as stream:
                return stream.getnframes() / stream.getframerate()
        except (OSError, EOFError, wave.Error):
            raise VoiceConversionError("VOICE_AUDIO_INVALID") from None
    probe = Path(ffmpeg_path).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    raw = _run([str(probe), "-v", "error", "-select_streams", "a:0", "-show_entries",
                "format=duration", "-of", "json", str(path)], "VOICE_AUDIO_INVALID")
    try:
        return float(json.loads(raw)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        raise VoiceConversionError("VOICE_AUDIO_INVALID") from None


def _stems(first, second, output, *, ffmpeg_path):
    if ffmpeg_path is None or not Path(ffmpeg_path).is_file():
        raise VoiceConversionError("FFMPEG_UNAVAILABLE")
    duration = _duration(first, ffmpeg_path)
    if abs(duration - _duration(second, ffmpeg_path)) > 0.25:
        raise VoiceConversionError("VOICE_STEM_DURATION_MISMATCH")
    # Keep the source timeline and stereo piano image; never concatenate stems.
    filters = ("[0:a]aresample=44100:first_pts=0,aformat=channel_layouts=stereo[a];"
               "[1:a]aresample=44100:first_pts=0,aformat=channel_layouts=stereo,apad[b];"
               "[a][b]amix=inputs=2:duration=first:normalize=0:weights='1 1',"
               "alimiter=limit=0.95:level=false:latency=true")
    _run([str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y", "-i", str(first),
          "-i", str(second), "-filter_complex", filters, "-c:a", "pcm_f32le", str(output)],
         "VOICE_STEM_MIX_FAILED")


def mix_song_voice(vocals, accompaniment, output, *, ffmpeg_path):
    _stems(accompaniment, vocals, output, ffmpeg_path=ffmpeg_path)


@functools.lru_cache(maxsize=256)
def _hash_file(path, size, mtime_ns):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def voice_conversion_fingerprint(environment):
    try:
        root, python = _runtime(environment)
    except VoiceConversionError:
        return {"status": "missing", "contract": "soulx-singer-svc-fp16-v1"}
    files = [python, _worker_path()]
    files += sorted(p for folder in (root / "source", root / "models") if folder.exists()
                    for p in folder.rglob("*") if p.is_file() and p.suffix in
                    {".py", ".pth", ".pt", ".bin", ".safetensors", ".yaml", ".yml", ".json"})
    result = {}
    for p in files:
        if p.is_file():
            stat = p.stat()
            key = str(p.relative_to(root)) if p.is_relative_to(root) else ("python" if p == python else "worker")
            result[key] = _hash_file(str(p), stat.st_size, stat.st_mtime_ns)
    return {"files": result, "contract": "soulx-singer-svc-fp16-v1", "steps": 32,
            "cfg": 1.0, "f0": True, "pitch_shift": 0, "auto_f0": False, "seed": 200717}


def voice_conversion_runtime_ready(environment):
    """Require the singing model closure, without loading models or networking."""
    try:
        root, _ = _runtime(environment)
        required = ("models/official/model-svc.pt", "models/preprocess/rmvpe/rmvpe.pt",
                    "models/whisper-base/config.json", "models/whisper-base/model.safetensors",
                    "models/whisper-base/preprocessor_config.json",
                    "source/soulxsinger/config/soulxsinger.yaml",
                    "source/preprocess/tools/f0_extraction.py")
        return all((root / name).is_file() and (root / name).stat().st_size > 0 for name in required)
    except (OSError, ValueError, VoiceConversionError):
        return False
