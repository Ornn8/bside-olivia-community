"""One-shot MiniMax Music 3 worker using an isolated local ComfyUI process."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.minimax_profile import (  # noqa: E402
    MiniMaxInferenceProfile,
    MiniMaxProfileError,
    minimax_profile_from_mapping,
)


_ALLOWED_DURATIONS = frozenset({40, 60})
_INFERENCE_TIMEOUT_SECONDS = 14400.0
_EXPECTED_LYRIC_LINES = {40: 12, 60: 16}
_SONG_TAGS = ("[Intro]", "[Verse]", "[Chorus]", "[Outro]")
_TAG_LINE = re.compile(r"^\[[A-Za-z][A-Za-z0-9_-]{0,31}\]$")
_CAPTION_HEADINGS = ("### Global Metadata", "### Vocal Details", "### Arrangement")
_NEGATIVE_SYNTAX = re.compile(
    r"\b(?:no|not|without|avoid|exclude|excluding|never|absence|lack)\b",
    flags=re.IGNORECASE,
)
_DISALLOWED_CAPTION_TERMS = re.compile(
    r"\b(?:"
    r"r&b|rnb|neo[- ]?soul|soul|jazz|gospel|cinematic|heritage|folk|pop|"
    r"electronic|ambient|orchestral|orchestra|groove|swing|syncopated|"
    r"backbeat|melisma|riff|ad[- ]?lib|drums?|percussion|bass|guitars?|"
    r"strings?|cello|violins?|synth(?:esizer)?s?|pads?|choir|guzheng|"
    r"erhu|pipa|dizi|flute"
    r")\b",
    flags=re.IGNORECASE,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comfy-root", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--request-json")
    source.add_argument("--batch-json")
    parser.add_argument("--output")
    parser.add_argument("--metrics-json")
    parser.add_argument("--reserve-vram", type=float, default=0.5)
    parser.add_argument("--cache-lru", type=int, default=12)
    parser.add_argument("--vram-mode", choices=("dynamic", "low"), default="dynamic")
    return parser


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request_json(url: str, *, payload: dict | None = None, timeout: float = 30.0) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise RuntimeError("MINIMAX_MUSIC3_PROTOCOL_INVALID")
    return result


def _duration(value: object) -> int:
    if type(value) is not int or value not in _ALLOWED_DURATIONS:
        raise RuntimeError("MINIMAX_MUSIC3_DURATION_INVALID")
    return value


def _bounded_text(value: object, *, code: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(code)
    normalized = value.strip().replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized.encode("utf-8")) > max_bytes or any(
        ord(character) < 32 and character not in {"\n", "\t"}
        for character in normalized
    ):
        raise RuntimeError(code)
    return normalized


def _lyrics(value: object, duration: int) -> str:
    normalized = _bounded_text(
        value,
        code="MINIMAX_MUSIC3_LYRICS_REQUIRED",
        max_bytes=8192,
    )
    lines = tuple(line.strip() for line in normalized.split("\n") if line.strip())
    tags = tuple(line for line in lines if _TAG_LINE.fullmatch(line))
    if tags != _SONG_TAGS:
        raise RuntimeError("MINIMAX_MUSIC3_LYRICS_INVALID")
    lyric_lines = tuple(line for line in lines if not _TAG_LINE.fullmatch(line))
    if len(lyric_lines) != _EXPECTED_LYRIC_LINES[duration]:
        raise RuntimeError("MINIMAX_MUSIC3_LYRICS_INVALID")
    return normalized


def _caption(value: object) -> str:
    normalized = _bounded_text(
        value,
        code="MINIMAX_MUSIC3_CAPTION_REQUIRED",
        max_bytes=8192,
    )
    headings = tuple(re.findall(r"^### .+$", normalized, flags=re.MULTILINE))
    if headings != _CAPTION_HEADINGS:
        raise RuntimeError("MINIMAX_MUSIC3_CAPTION_INVALID")
    if _NEGATIVE_SYNTAX.search(normalized):
        raise RuntimeError("MINIMAX_MUSIC3_CAPTION_NEGATIVE_SYNTAX")
    if _DISALLOWED_CAPTION_TERMS.search(normalized):
        raise RuntimeError("MINIMAX_MUSIC3_CAPTION_DISALLOWED_TERM")
    return normalized


def _validated_request(
    request: object,
) -> tuple[int, str, str, MiniMaxInferenceProfile]:
    if not isinstance(request, dict):
        raise RuntimeError("MINIMAX_MUSIC3_REQUEST_INVALID")
    duration = _duration(request.get("max_duration"))
    lyrics = _lyrics(request.get("lyrics"), duration)
    caption = _caption(request.get("caption"))
    try:
        profile = minimax_profile_from_mapping(request.get("inference_profile"))
    except MiniMaxProfileError as exc:
        raise RuntimeError(str(exc) or "MINIMAX_PROFILE_INVALID") from exc
    return duration, lyrics, caption, profile


def _graph(request: dict[str, object], *, filename_prefix: str) -> dict[str, object]:
    duration, lyrics, caption, profile = _validated_request(request)
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "minimax_music3_dit_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
            "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_music3_dav.safetensors"}},
        "4": {"class_type": "MiniMaxMusic3TextEncode", "inputs": {
            "clip": ["2", 0], "caption": caption, "lyrics": lyrics, "seed": profile.seed,
            "max_duration": duration, "cfg_scale": profile.text_cfg_scale,
            "top_k": profile.top_k}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptyMiniMaxMusic3LatentAudio", "inputs": {
            "seconds": ["4", 1], "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "seed": profile.seed, "steps": profile.steps,
            "cfg": profile.sampler_cfg_scale, "sampler_name": profile.sampler_name,
            "scheduler": profile.scheduler, "positive": ["4", 0],
            "negative": ["5", 0], "latent_image": ["6", 0],
            "denoise": profile.denoise}},
        "8": {"class_type": "VAEDecodeAudioTiled", "inputs": {
            "samples": ["7", 0], "vae": ["3", 0], "tile_size": 1536, "overlap": 64}},
        "9": {"class_type": "SaveAudio", "inputs": {
            "audio": ["8", 0], "filename_prefix": filename_prefix}},
    }


def _load_jobs(args: argparse.Namespace) -> list[tuple[Path, Path]]:
    if args.request_json:
        if not args.output:
            raise SystemExit("MINIMAX_MUSIC3_OUTPUT_REQUIRED")
        return [(Path(args.request_json).resolve(), Path(args.output).resolve())]
    if args.output:
        raise SystemExit("MINIMAX_MUSIC3_BATCH_OUTPUT_CONFLICT")
    batch_path = Path(args.batch_json).resolve()
    value = json.loads(batch_path.read_text(encoding="utf-8"))
    items = value.get("jobs") if isinstance(value, dict) else None
    if not isinstance(items, list) or not items:
        raise SystemExit("MINIMAX_MUSIC3_BATCH_INVALID")
    jobs: list[tuple[Path, Path]] = []
    for item in items:
        if not isinstance(item, dict):
            raise SystemExit("MINIMAX_MUSIC3_BATCH_INVALID")
        request_value = Path(str(item.get("request_json", "")))
        output_value = Path(str(item.get("output", "")))
        request_path = request_value if request_value.is_absolute() else batch_path.parent / request_value
        output_path = output_value if output_value.is_absolute() else batch_path.parent / output_value
        jobs.append((request_path.resolve(), output_path.resolve()))
    return jobs


def _read_request(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("MINIMAX_MUSIC3_REQUEST_INVALID") from exc
    if not isinstance(value, dict):
        raise RuntimeError("MINIMAX_MUSIC3_REQUEST_INVALID")
    return value


def _gpu_sample() -> dict[str, float] | None:
    command = [
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used,power.draw",
        "--format=csv,noheader,nounits",
    ]
    try:
        line = subprocess.check_output(
            command, stderr=subprocess.DEVNULL, text=True, timeout=5
        ).splitlines()[0]
        utilization, memory_mb, power_watts = (float(part.strip()) for part in line.split(","))
        return {
            "utilization_percent": utilization,
            "memory_used_mb": memory_mb,
            "power_watts": power_watts,
        }
    except Exception:
        return None


def _copy_result(
    *,
    base_url: str,
    prompt_id: str,
    generated_root: Path,
    output: Path,
    gpu_samples: list[dict[str, float]],
    server_process: subprocess.Popen,
) -> None:
    deadline = time.monotonic() + _INFERENCE_TIMEOUT_SECONDS
    next_gpu_sample = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_gpu_sample:
            sample = _gpu_sample()
            if sample is not None:
                gpu_samples.append(sample)
            next_gpu_sample = now + 10.0
        if server_process.poll() is not None:
            raise RuntimeError("MINIMAX_MUSIC3_SERVER_EXITED")
        try:
            history = _request_json(f"{base_url}/history/{prompt_id}")
        except (OSError, TimeoutError):
            # Long GPU kernels can make ComfyUI's local HTTP loop temporarily
            # unresponsive.  The inference is still healthy while its process
            # remains alive, so keep polling until the overall deadline.
            time.sleep(2.0)
            continue
        item = history.get(prompt_id)
        if isinstance(item, dict):
            outputs = item.get("outputs")
            audio_items = outputs.get("9", {}).get("audio", []) if isinstance(outputs, dict) else []
            if not audio_items or not isinstance(audio_items[0], dict):
                raise RuntimeError("MINIMAX_MUSIC3_OUTPUT_INVALID")
            filename = Path(str(audio_items[0].get("filename", ""))).name
            subfolder = Path(str(audio_items[0].get("subfolder", "")))
            source = generated_root / subfolder / filename
            if not source.is_file():
                raise RuntimeError("MINIMAX_MUSIC3_OUTPUT_MISSING")
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
            return
        time.sleep(2.0)
    raise RuntimeError("MINIMAX_MUSIC3_TIMEOUT")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


_COMFY_BOOTSTRAP = (
    "import runpy,sys; "
    "root,entry,*args=sys.argv[1:]; "
    "sys.path.insert(0, root); "
    "sys.argv=[entry,*args]; "
    "runpy.run_path(entry,run_name='__main__')"
)


def _comfy_command(
    *,
    comfy_root: Path,
    port: int,
    cache_lru: int,
    reserve_vram: float,
    generated_root: Path,
    temp_root: Path,
    vram_mode: str,
) -> list[str]:
    command = [
        str(Path(sys.executable)),
        "-c",
        _COMFY_BOOTSTRAP,
        str(comfy_root),
        str(comfy_root / "main.py"),
        "--listen", "127.0.0.1",
        "--port", str(port),
        "--cache-lru", str(max(3, cache_lru)),
        "--reserve-vram", str(max(0.25, reserve_vram)),
        "--disable-auto-launch",
        "--disable-all-custom-nodes",
        "--output-directory", str(generated_root),
        "--temp-directory", str(temp_root),
    ]
    if vram_mode == "low":
        command.append("--lowvram")
    return command


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    comfy_root = Path(args.comfy_root).resolve()
    jobs = _load_jobs(args)
    if not (comfy_root / "main.py").is_file() or any(not job[0].is_file() for job in jobs):
        raise SystemExit("MINIMAX_MUSIC3_INPUT_UNAVAILABLE")
    try:
        requests = [_read_request(request_path) for request_path, _ in jobs]
        validated = [_validated_request(request) for request in requests]
    except RuntimeError as exc:
        raise SystemExit(str(exc) or "MINIMAX_MUSIC3_REQUEST_INVALID") from exc

    metrics_path = Path(args.metrics_json).resolve() if args.metrics_json else None
    owner_root = metrics_path.parent if metrics_path else jobs[0][1].parent
    work_root = owner_root / ".minimax-music3-batch-work"
    generated_root = work_root / "output"
    temp_root = work_root / "temp"
    log_path = work_root / "comfy.log"
    generated_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    command = _comfy_command(
        comfy_root=comfy_root,
        port=port,
        cache_lru=args.cache_lru,
        reserve_vram=args.reserve_vram,
        generated_root=generated_root,
        temp_root=temp_root,
        vram_mode=args.vram_mode,
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    started_at = time.monotonic()
    metrics: dict[str, object] = {
        "schema_version": 2,
        "vram_mode": args.vram_mode,
        "reserve_vram_gb": max(0.25, args.reserve_vram),
        "cache_lru": max(3, args.cache_lru),
        "jobs": [],
    }
    gpu_samples: list[dict[str, float]] = []
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=comfy_root,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 180.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("MINIMAX_MUSIC3_SERVER_EXITED")
                try:
                    _request_json(f"{base_url}/object_info", timeout=2.0)
                    break
                except Exception:
                    time.sleep(1.0)
            else:
                raise RuntimeError("MINIMAX_MUSIC3_SERVER_TIMEOUT")
            metrics["server_startup_seconds"] = round(time.monotonic() - started_at, 3)

            for index, ((request_path, output), request, valid) in enumerate(
                zip(jobs, requests, validated),
                start=1,
            ):
                _duration_value, _lyrics_value, _caption_value, profile = valid
                job_started_at = time.monotonic()
                filename_prefix = f"audio/{index:02d}_{output.stem}"
                submitted = _request_json(
                    f"{base_url}/prompt",
                    payload={
                        "prompt": _graph(request, filename_prefix=filename_prefix),
                        "client_id": "olivia-local",
                    },
                )
                prompt_id = str(submitted.get("prompt_id", ""))
                if not prompt_id:
                    raise RuntimeError("MINIMAX_MUSIC3_PROMPT_REJECTED")
                _copy_result(
                    base_url=base_url,
                    prompt_id=prompt_id,
                    generated_root=generated_root,
                    output=output,
                    gpu_samples=gpu_samples,
                    server_process=process,
                )
                metrics["jobs"].append({
                    "request_json": str(request_path),
                    "output": str(output),
                    "inference_profile": profile.to_dict(),
                    "elapsed_seconds": round(time.monotonic() - job_started_at, 3),
                })
        finally:
            _stop(process)
    if any(not output.is_file() or output.stat().st_size == 0 for _, output in jobs):
        raise SystemExit("MINIMAX_MUSIC3_OUTPUT_MISSING")
    metrics["total_elapsed_seconds"] = round(time.monotonic() - started_at, 3)
    if gpu_samples:
        metrics["gpu_samples"] = {
            "count": len(gpu_samples),
            "utilization_percent_mean": round(
                sum(x["utilization_percent"] for x in gpu_samples) / len(gpu_samples),
                2,
            ),
            "utilization_percent_max": max(x["utilization_percent"] for x in gpu_samples),
            "memory_used_mb_max": max(x["memory_used_mb"] for x in gpu_samples),
            "power_watts_mean": round(
                sum(x["power_watts"] for x in gpu_samples) / len(gpu_samples),
                2,
            ),
            "power_watts_max": max(x["power_watts"] for x in gpu_samples),
        }
    if metrics_path:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.copyfile(log_path, metrics_path.with_suffix(".comfy.log"))
    shutil.rmtree(work_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
