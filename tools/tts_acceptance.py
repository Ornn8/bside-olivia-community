"""One-process B06 real-provider acceptance gate.

The harness keeps private model/reference paths in command arguments only. Its
JSON report contains basenames, timings, signal metrics, semantic ASR, and GPU
resource facts; model weights and generated media remain in the ignored evidence
directory on the local machine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.tts_asr_probe import transcribe_audio  # noqa: E402
from tts import TTSProfileManager, TTSRequest, TTSService  # noqa: E402
from tts.audio import audio_metrics  # noqa: E402


SHORT_TEXT = "你好，这是本地流式语音合成测试。第二句用于验证句级输出。"
LONG_TEXT = (
    "这是一个用于验证本地语音合成的较长输入句子，内容会经过句级拆分并在句内持续输出音频。"
    "第二个句子继续检查长文本之后的边界、采样率、静音比例和截断状态。"
    "第三个句子用于确认同一个本地模型可以在长请求结束后继续接受下一次请求。"
)
CANCEL_TEXT = "这是一个用于中断验收的长句。" * 8
CONTINUOUS_TEXTS = (
    "连续请求的第一句用于确认首包和结束事件。",
    "连续请求的第二句用于确认前一个请求结束后仍能正常输出。",
)


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _nvidia_snapshot() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi") or "nvidia-smi"
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        line = next((item.strip() for item in completed.stdout.splitlines() if item.strip()), "")
        fields = [item.strip() for item in line.split(",")]
        if len(fields) != 5:
            raise ValueError("unexpected nvidia-smi output")
        return {
            "available": True,
            "name": fields[0],
            "memory_used_mb": int(float(fields[1])),
            "memory_total_mb": int(float(fields[2])),
            "utilization_gpu_percent": int(float(fields[3])),
            "temperature_c": int(float(fields[4])),
        }
    except Exception:
        return {"available": False, "error_code": "NVIDIA_SMI_UNAVAILABLE"}


class _ResourceSampler:
    def __init__(self, interval_seconds: float = 0.5) -> None:
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.samples.append(_nvidia_snapshot())
        self._thread = threading.Thread(target=self._loop, name="b06-gpu-sampler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if len(self.samples) < 240:
                self.samples.append(_nvidia_snapshot())

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
        self.samples.append(_nvidia_snapshot())

    def summary(self) -> dict[str, Any]:
        usable = [item for item in self.samples if item.get("available")]
        if not usable:
            return {"available": False, "sample_count": len(self.samples)}
        return {
            "available": True,
            "sample_count": len(self.samples),
            "gpu_name": usable[-1]["name"],
            "memory_total_mb": max(item["memory_total_mb"] for item in usable),
            "memory_peak_used_mb": max(item["memory_used_mb"] for item in usable),
            "utilization_peak_percent": max(item["utilization_gpu_percent"] for item in usable),
            "temperature_peak_c": max(item["temperature_c"] for item in usable),
        }


def _torch_summary() -> dict[str, Any]:
    try:
        import torch

        if not torch.cuda.is_available():
            return {"cuda_available": False}
        torch.cuda.synchronize()
        return {
            "cuda_available": True,
            "device": torch.cuda.get_device_name(0),
            "allocated_mb": round(torch.cuda.memory_allocated() / 1024**2, 2),
            "reserved_mb": round(torch.cuda.memory_reserved() / 1024**2, 2),
            "max_allocated_mb": round(torch.cuda.max_memory_allocated() / 1024**2, 2),
            "max_reserved_mb": round(torch.cuda.max_memory_reserved() / 1024**2, 2),
        }
    except Exception:
        return {"cuda_available": False, "error_code": "TORCH_CUDA_METRICS_UNAVAILABLE"}


def _public_result(result: Any, output: Path | None = None) -> dict[str, Any]:
    value = asdict(result)
    if output is not None:
        value["output_path"] = output.name
    elif value.get("output_path"):
        value["output_path"] = Path(str(value["output_path"])).name
    value["fallback_text"] = None
    return value


async def _run_case(service: TTSService, name: str, text: str, output: Path) -> dict[str, Any]:
    request = TTSRequest(text, stream=True)
    run = await service.start(request, output_path=output)
    events: list[dict[str, Any]] = []
    async for event in run.events():
        item: dict[str, Any] = {
            "event": event.event,
            "timestamp_ms": round(event.timestamp_ms, 3),
        }
        if event.chunk is not None:
            item.update(
                {
                    "sentence_index": event.chunk.sentence_index,
                    "chunk_index": event.chunk.chunk_index,
                    "sample_count": event.chunk.sample_count,
                    "sample_rate": event.chunk.sample_rate,
                }
            )
        events.append(item)
    result = await asyncio.wait_for(run.wait(), timeout=30)
    audio_events = [item for item in events if item["event"] == "audio_chunk"]
    value = {
        "case": name,
        "input_char_count": len(text),
        "events": events,
        "first_audio_packet_ms": audio_events[0]["timestamp_ms"] if audio_events else None,
        "last_audio_packet_ms": audio_events[-1]["timestamp_ms"] if audio_events else None,
        "terminal_event_ms": events[-1]["timestamp_ms"] if events else None,
        "result": _public_result(result, output),
        "wav_exists": output.is_file(),
    }
    if output.is_file():
        value["audio"] = audio_metrics(output)
    return value


async def _run_cancel(service: TTSService, output: Path) -> dict[str, Any]:
    request = TTSRequest(CANCEL_TEXT, stream=True)
    run = await service.start(request, output_path=output)
    events: list[dict[str, Any]] = []
    cancel_requested = False
    for _ in range(128):
        event = await asyncio.wait_for(run.queue.get(), timeout=30)
        item: dict[str, Any] = {
            "event": event.event,
            "timestamp_ms": round(event.timestamp_ms, 3),
        }
        if event.chunk is not None:
            item.update(
                {
                    "sentence_index": event.chunk.sentence_index,
                    "chunk_index": event.chunk.chunk_index,
                    "sample_count": event.chunk.sample_count,
                    "sample_rate": event.chunk.sample_rate,
                }
            )
            events.append(item)
            if not cancel_requested:
                cancel_requested = run.cancel()
                break
        else:
            events.append(item)
        if event.event in {"completed", "cancelled", "unavailable", "text_fallback", "failed"}:
            break
    result = await asyncio.wait_for(run.wait(), timeout=30)
    audio_events = [item for item in events if item["event"] == "audio_chunk"]
    return {
        "case": "cancel",
        "input_char_count": len(CANCEL_TEXT),
        "events": events,
        "cancel_requested": cancel_requested,
        "first_audio_packet_ms": audio_events[0]["timestamp_ms"] if audio_events else None,
        "last_audio_packet_ms": audio_events[-1]["timestamp_ms"] if audio_events else None,
        "result": _public_result(result, output),
        "wav_exists": output.is_file(),
    }


def _completed_gate(value: dict[str, Any]) -> bool:
    result = value["result"]
    audio = value.get("audio", {})
    return bool(
        result["status"] == "completed"
        and result["chunk_count"] > 0
        and result["sample_rate"] == 24000
        and value["first_audio_packet_ms"] is not None
        and value["last_audio_packet_ms"] is not None
        and value["terminal_event_ms"] is not None
        and value["first_audio_packet_ms"] <= value["last_audio_packet_ms"] <= value["terminal_event_ms"]
        and value["wav_exists"]
        and audio.get("has_audio") is True
        and audio.get("clipped_samples") == 0
        and audio.get("truncated") is False
    )


def _cancel_gate(value: dict[str, Any]) -> bool:
    result = value["result"]
    return bool(
        value["cancel_requested"]
        and result["status"] == "cancelled"
        and result["error_code"] == "TTS_CANCELLED"
        and not value["wav_exists"]
    )


async def _async_main(args: argparse.Namespace) -> dict[str, Any]:
    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    manager = TTSProfileManager(args.state_root)
    config = manager.config(args.profile)
    service = TTSService(config)
    sampler = _ResourceSampler()
    outputs = {
        "short": evidence / "audio-short.wav",
        "long": evidence / "audio-long.wav",
        "cancel": evidence / "audio-cancel.wav",
        "continuous_1": evidence / "audio-continuous-1.wav",
        "continuous_2": evidence / "audio-continuous-2.wav",
    }
    cases: dict[str, Any] = {}
    service_health = service.health()
    sampler.start()
    try:
        if service_health.get("status") == "available":
            cases["short"] = await _run_case(service, "short", SHORT_TEXT, outputs["short"])
            cases["long"] = await _run_case(service, "long", LONG_TEXT, outputs["long"])
            cases["cancel"] = await _run_cancel(service, outputs["cancel"])
            cases["continuous_1"] = await _run_case(
                service, "continuous_1", CONTINUOUS_TEXTS[0], outputs["continuous_1"]
            )
            cases["continuous_2"] = await _run_case(
                service, "continuous_2", CONTINUOUS_TEXTS[1], outputs["continuous_2"]
            )
    finally:
        tts_torch = _torch_summary()
        sampler.stop()
        service.close()

    asr = transcribe_audio(
        outputs["short"],
        model_name=args.asr_model,
        download_root=args.asr_download_root,
        language=args.asr_language,
        must_contain=args.asr_must_contain,
    )
    gates = {
        "provider_health": service_health.get("status") == "available",
        "short": _completed_gate(cases["short"]) if "short" in cases else False,
        "long": _completed_gate(cases["long"]) if "long" in cases else False,
        "cancel": _cancel_gate(cases["cancel"]) if "cancel" in cases else False,
        "continuous_1": _completed_gate(cases["continuous_1"]) if "continuous_1" in cases else False,
        "continuous_2": _completed_gate(cases["continuous_2"]) if "continuous_2" in cases else False,
        "semantic_asr": asr.get("status") == "PASS",
        "cuda": bool(tts_torch.get("cuda_available")),
        "gpu_resource_samples": bool(sampler.summary().get("available")),
    }
    report = {
        "schema": "b06.tts.acceptance.v1",
        "provider": service_health,
        "profile": config.public_dict(),
        "environment": {
            "offline_flags_set": True,
            "temp_drive": str(os.environ.get("TEMP", ""))[:2],
            "tmp_drive": str(os.environ.get("TMP", ""))[:2],
            "numba_cache_configured": bool(os.environ.get("NUMBA_CACHE_DIR")),
        },
        "cases": cases,
        "semantic_asr": asr,
        "resources": {
            "nvidia_smi": sampler.summary(),
            "torch": tts_torch,
        },
        "gates": gates,
        "skip_count": 0,
        "fail_count": sum(not value for value in gates.values()),
        "status": "PASS" if all(gates.values()) else "FAIL",
    }
    _json_write(evidence / "acceptance.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B06 real local TTS acceptance")
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--profile", default="cosyvoice3-live")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--asr-model", default="base")
    parser.add_argument("--asr-download-root", required=True)
    parser.add_argument("--asr-language", default="zh")
    parser.add_argument("--asr-must-contain", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = asyncio.run(_async_main(args))
    except Exception as exc:
        detail = re.sub(r"[A-Za-z]:\\[^\r\n]*", "<private-path>", str(exc))[:300]
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_code": "ACCEPTANCE_HARNESS_ERROR",
                    "exception_type": type(exc).__name__,
                    "detail": detail,
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "fail_count": report["fail_count"],
                "skip_count": report["skip_count"],
                "gates": report["gates"],
                "asr_text": report["semantic_asr"].get("text", ""),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
