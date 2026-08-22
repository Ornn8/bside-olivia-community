"""One-shot MiniMax Music 3 worker using an isolated local ComfyUI process."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path


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


def _graph(request: dict[str, object], *, filename_prefix: str) -> dict[str, object]:
    content = " ".join(str(request.get("content", "")).split())[:180]
    reply = " ".join(str(request.get("reply_text", "")).split())[:180]
    duration = int(request.get("max_duration", 0))
    if duration not in {90, 118}:
        raise RuntimeError("MINIMAX_MUSIC3_DURATION_INVALID")
    seed = int(request.get("seed", 200717))
    fallback_lyrics = (
        "[Intro]\n[Verse]\n"
        f"{content}\n{reply}\n"
        "今天是搬进新家的第一天\n"
        "窗边的光落在琴键\n"
        "[Chorus]\n"
        "愿往后的每一封信\n"
        "都能在这里被你听见\n"
        "[Outro]"
    )
    fallback_caption = (
        "Global Metadata: intimate Mandarin cinematic piano ballad, 68 BPM, D minor, "
        "tender and reflective, warm room, restrained emotional arc, studio-quality natural sound. "
        "Vocal Details: young adult Chinese female voice, clear gentle mezzo-soprano, intimate close-mic, "
        "natural Mandarin pronunciation, controlled breath, no exaggerated vibrato, no childlike tone, "
        "no imitation of any real singer. Arrangement: solo grand piano opening, soft strings gradually enter, "
        "subtle cello and room reverb, sparse percussion near the final chorus, clean piano ending."
    )
    lyrics = str(request.get("lyrics", "")).strip() or fallback_lyrics
    caption = str(request.get("caption", "")).strip() or fallback_caption
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "minimax_music3_dit_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
            "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_music3_dav.safetensors"}},
        "4": {"class_type": "MiniMaxMusic3TextEncode", "inputs": {
            "clip": ["2", 0], "caption": caption, "lyrics": lyrics, "seed": seed,
            "max_duration": duration, "cfg_scale": 1.5, "top_k": 50}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptyMiniMaxMusic3LatentAudio", "inputs": {
            "seconds": ["4", 1], "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "seed": seed, "steps": 30, "cfg": 1.5,
            "sampler_name": "euler", "scheduler": "simple", "positive": ["4", 0],
            "negative": ["5", 0], "latent_image": ["6", 0], "denoise": 1.0}},
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
) -> None:
    deadline = time.monotonic() + 3600.0
    next_gpu_sample = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_gpu_sample:
            sample = _gpu_sample()
            if sample is not None:
                gpu_samples.append(sample)
            next_gpu_sample = now + 10.0
        history = _request_json(f"{base_url}/history/{prompt_id}")
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    comfy_root = Path(args.comfy_root).resolve()
    jobs = _load_jobs(args)
    if not (comfy_root / "main.py").is_file() or any(not job[0].is_file() for job in jobs):
        raise SystemExit("MINIMAX_MUSIC3_INPUT_UNAVAILABLE")
    requests = [json.loads(request_path.read_text(encoding="utf-8")) for request_path, _ in jobs]
    if any(not isinstance(request, dict) for request in requests):
        raise SystemExit("MINIMAX_MUSIC3_REQUEST_INVALID")

    metrics_path = Path(args.metrics_json).resolve() if args.metrics_json else None
    owner_root = metrics_path.parent if metrics_path else jobs[0][1].parent
    work_root = owner_root / ".minimax-music3-batch-work"
    generated_root = work_root / "output"
    temp_root = work_root / "temp"
    log_path = work_root / "comfy.log"
    generated_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    command = [
        str(Path(__import__("sys").executable)),
        str(comfy_root / "main.py"),
        "--listen", "127.0.0.1",
        "--port", str(port),
        "--cache-lru", str(max(3, args.cache_lru)),
        "--reserve-vram", str(max(0.25, args.reserve_vram)),
        "--disable-auto-launch",
        "--disable-all-custom-nodes",
        "--output-directory", str(generated_root),
        "--temp-directory", str(temp_root),
    ]
    if args.vram_mode == "low":
        command.append("--lowvram")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    started_at = time.monotonic()
    metrics: dict[str, object] = {
        "schema_version": 1,
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

            for index, ((request_path, output), request) in enumerate(zip(jobs, requests), start=1):
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
                )
                metrics["jobs"].append({
                    "request_json": str(request_path),
                    "output": str(output),
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
            "utilization_percent_mean": round(sum(x["utilization_percent"] for x in gpu_samples) / len(gpu_samples), 2),
            "utilization_percent_max": max(x["utilization_percent"] for x in gpu_samples),
            "memory_used_mb_max": max(x["memory_used_mb"] for x in gpu_samples),
            "power_watts_mean": round(sum(x["power_watts"] for x in gpu_samples) / len(gpu_samples), 2),
            "power_watts_max": max(x["power_watts"] for x in gpu_samples),
        }
    if metrics_path:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copyfile(log_path, metrics_path.with_suffix(".comfy.log"))
    shutil.rmtree(work_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
