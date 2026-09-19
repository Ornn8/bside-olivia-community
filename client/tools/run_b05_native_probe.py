"""Run a reproducible low-level B05 native HTTP and synchronous WS probe.

The runtime, model, CUDA toolkit, and audio fixture are deliberately supplied
as external paths.  This keeps large local assets in ignored D:/F: evidence
while making the request ordering and raw protocol observations reviewable.
The probe never turns a missing ``session.created`` or transcript into PASS.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
from urllib.parse import urlsplit, urlunsplit
import urllib.request
import uuid
import wave
from typing import Any

from websockets.sync.client import connect as sync_connect


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REFERENCE = (
    "嗨，今天过得怎么样？我一直在等你写信给我呢。"
    "听说外面的世界下雪了，你那边冷吗？记得多穿一点。"
)
MODEL_REVISION = "1c8deaecc64b91f034d73e08dd8b64625eb3395d"
MODEL_SHA256 = "a5c435f294eea8f88ce68dd27b8c3bfea7f777cb2fbba04fcd30eaa555f429ae"
MAX_CER = 0.20
MAX_WER = 0.20
RUNTIME_REVISION = "1118951337094db3b362fbf1b27e871696f10590"
FRAME_BYTES = 3200  # 100 ms of mono PCM16 at 16 kHz.


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_wav(path: Path) -> tuple[bytes, int, int, int, str]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        sample_rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
        sample_count = audio.getnframes()
        compression = audio.getcomptype()
    if channels != 1 or sample_width != 2 or compression != "NONE":
        raise ValueError(
            "probe requires mono PCM16 WAV; "
            f"channels={channels} sample_width={sample_width} compression={compression}"
        )
    return frames, sample_rate, channels, sample_width, compression


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )


def wer_tokens(value: str) -> list[str]:
    normalized = normalize_text(value)
    tokens: list[str] = []
    ascii_run = ""
    for character in normalized:
        if "\u4e00" <= character <= "\u9fff":
            if ascii_run:
                tokens.append(ascii_run)
                ascii_run = ""
            tokens.append(character)
        else:
            ascii_run += character
    if ascii_run:
        tokens.append(ascii_run)
    return tokens


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for ref_index, ref_token in enumerate(reference, start=1):
        current = [ref_index]
        for hyp_index, hyp_token in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[hyp_index] + 1,
                    previous[hyp_index - 1] + (ref_token != hyp_token),
                )
            )
        previous = current
    return previous[-1]


def score(reference: str, hypothesis: str) -> dict[str, object]:
    reference_chars = list(normalize_text(reference))
    hypothesis_chars = list(normalize_text(hypothesis))
    reference_words = wer_tokens(reference)
    hypothesis_words = wer_tokens(hypothesis)
    cer_distance = edit_distance(reference_chars, hypothesis_chars)
    wer_distance = edit_distance(reference_words, hypothesis_words)
    return {
        "reference": reference,
        "hypothesis": hypothesis,
        "normalization": "Unicode NFKC + casefold; punctuation and whitespace removed",
        "wer_tokenization": "one CJK codepoint per token; contiguous non-CJK alphanumerics are one token",
        "reference_char_count": len(reference_chars),
        "hypothesis_char_count": len(hypothesis_chars),
        "reference_token_count": len(reference_words),
        "hypothesis_token_count": len(hypothesis_words),
        "cer_edit_distance": cer_distance,
        "wer_edit_distance": wer_distance,
        "cer": cer_distance / len(reference_chars) if reference_chars else None,
        "wer": wer_distance / len(reference_words) if reference_words else None,
    }


def event_type(value: object) -> str:
    return str(value.get("type", "")) if isinstance(value, dict) else ""


def event_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("transcript", "delta", "text"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    for key in ("item", "content", "data", "response"):
        candidate = value.get(key)
        if isinstance(candidate, dict):
            found = event_text(candidate)
            if found:
                return found
        if isinstance(candidate, list):
            for item in candidate:
                found = event_text(item)
                if found:
                    return found
    return ""


def http_call(base_url: str, method: str, path: str, timeout: float = 8.0) -> dict[str, object]:
    request = urllib.request.Request(f"{base_url}{path}", method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {
                "status": response.status,
                "body": body,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return {
            "status": error.code,
            "body": body,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except Exception as error:  # pragma: no cover - host/runtime boundary
        return {
            "status": None,
            "body": "",
            "error": f"{type(error).__name__}: {error}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def wait_ready(base_url: str, timeout: float) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        result = http_call(base_url, "GET", "/ready", timeout=3.0)
        observations.append(result)
        try:
            payload = json.loads(str(result.get("body", "")))
        except json.JSONDecodeError:
            payload = {}
        if result.get("status") == 200 and payload.get("ready") is True:
            return observations
        time.sleep(0.25)
    return observations


def decode_message(message: object) -> dict[str, object]:
    if isinstance(message, bytes):
        return {"type": "binary", "bytes": len(message)}
    try:
        value = json.loads(message)
    except (TypeError, json.JSONDecodeError):
        return {"type": "invalid_json", "raw": str(message)}
    return value if isinstance(value, dict) else {"type": "json_value", "value": value}


def recv_event(
    ws: Any,
    events: list[dict[str, object]],
    started: float,
    timeout: float,
    phase: str,
) -> dict[str, object] | None:
    try:
        message = ws.recv(timeout=timeout)
    except Exception as error:  # timeout and close are both evidence
        events.append(
            {
                "direction": "client_observation",
                "phase": phase,
                "offset_s": round(time.perf_counter() - started, 6),
                "error": f"{type(error).__name__}: {error}",
            }
        )
        return None
    payload = decode_message(message)
    record = {
        "direction": "server",
        "phase": phase,
        "offset_s": round(time.perf_counter() - started, 6),
        "event": payload,
    }
    events.append(record)
    return payload


def send_json(ws: Any, events: list[dict[str, object]], started: float, payload: dict[str, object]) -> None:
    ws.send(json.dumps(payload, ensure_ascii=False))
    events.append(
        {
            "direction": "client",
            "offset_s": round(time.perf_counter() - started, 6),
            "message": payload,
        }
    )


def summarize_ws(
    endpoint: str,
    events: list[dict[str, object]],
    started: float,
    *,
    error: str | None,
    audio_bytes: int,
    sample_rate: int,
    committed: bool,
) -> dict[str, object]:
    server_events = [entry.get("event", {}) for entry in events if entry.get("direction") == "server"]
    processed_audio_ms = [
        round(float(value.get("audio_processed")) * 1000, 3)
        for value in server_events
        if isinstance(value, dict)
        and isinstance(value.get("audio_processed"), (int, float))
        and float(value.get("audio_processed")) >= 0
    ]
    native_words = [
        word
        for value in server_events
        if isinstance(value, dict) and isinstance(value.get("words"), list)
        for word in value["words"]
        if isinstance(word, dict)
    ]
    partials = [
        event_text(value)
        for value in server_events
        if event_type(value).endswith(".delta") and event_text(value)
    ]
    finals = [
        event_text(value)
        for value in server_events
        if event_type(value).endswith(".completed") and event_text(value)
    ]
    partial_offset = next(
        (
            float(entry["offset_s"])
            for entry in events
            if entry.get("direction") == "server"
            and event_type(entry.get("event", {})).endswith(".delta")
            and event_text(entry.get("event", {}))
        ),
        None,
    )
    final_offset = next(
        (
            float(entry["offset_s"])
            for entry in events
            if entry.get("direction") == "server"
            and event_type(entry.get("event", {})).endswith(".completed")
        ),
        None,
    )
    event_types = [event_type(entry.get("event", {})) for entry in events if entry.get("direction") == "server"]
    session_created = "session.created" in event_types
    final_text = finals[-1] if finals else ""
    return {
        "endpoint": endpoint,
        "event_types": event_types,
        "events": events,
        "session_created": session_created,
        "partial_count": len(partials),
        "partial_texts": partials,
        "final_count": len(finals),
        "completed_event_count": sum(value.endswith(".completed") for value in event_types),
        "final_texts": finals,
        "final_text": final_text,
        "first_partial_latency_ms": round(partial_offset * 1000, 3) if partial_offset is not None else None,
        "final_latency_ms": round(final_offset * 1000, 3) if final_offset is not None else None,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "audio_bytes": audio_bytes,
        "audio_duration_ms": round(audio_bytes / 2 / sample_rate * 1000, 3),
        "committed": committed,
        "error": error,
        "native_timing": {
            "audio_processed_event_count": len(processed_audio_ms),
            "audio_processed_last_ms": processed_audio_ms[-1] if processed_audio_ms else None,
            "word_timestamp_count": len(native_words),
            "word_timestamp_source": "native_words" if native_words else None,
        },
    }


def run_sync_session(
    endpoint: str,
    pcm: bytes,
    sample_rate: int,
    *,
    first_timeout: float,
    final_timeout: float,
    pace_s: float | None = None,
) -> dict[str, object]:
    started = time.perf_counter()
    events: list[dict[str, object]] = []
    error: str | None = None
    committed = False
    try:
        with sync_connect(
            endpoint,
            open_timeout=15,
            close_timeout=5,
            ping_interval=None,
            compression=None,
            proxy=None,
            max_size=None,
        ) as ws:
            first = recv_event(ws, events, started, first_timeout, "first_server_event")
            if first is None:
                error = "session.created missing within first_event_timeout"
            update = {
                "type": "session.update",
                "session": {
                    "sample_rate": sample_rate,
                    "language": "zh-CN",
                    "automatic_punctuation": True,
                    "verbatim": False,
                    "word_timestamps": True,
                    "endpointing_ms": 500,
                },
            }
            send_json(ws, events, started, update)
            updated = recv_event(ws, events, started, 4.0, "session_update_response")
            if updated is None and error is None:
                error = "session.updated missing within update_timeout"
            audio_started = time.perf_counter()
            for offset in range(0, len(pcm), FRAME_BYTES):
                ws.send(pcm[offset : offset + FRAME_BYTES])
                if pace_s is not None:
                    time.sleep(pace_s)
            events.append(
                {
                    "direction": "client_observation",
                    "phase": "audio_sent",
                    "offset_s": round(time.perf_counter() - started, 6),
                    "audio_send_duration_ms": round((time.perf_counter() - audio_started) * 1000, 3),
                    "frame_bytes": FRAME_BYTES,
                    "frame_count": (len(pcm) + FRAME_BYTES - 1) // FRAME_BYTES,
                }
            )
            send_json(ws, events, started, {"type": "input_audio_buffer.commit"})
            committed = True
            deadline = time.perf_counter() + final_timeout
            while time.perf_counter() < deadline:
                remaining = max(0.1, min(3.0, deadline - time.perf_counter()))
                event = recv_event(ws, events, started, remaining, "post_commit")
                if event is None:
                    break
                if event_type(event) == "error":
                    if error is None:
                        error = f"server_error: {event_text(event) or event}"
                    break
                if event_type(event).endswith(".completed"):
                    break
    except Exception as exc:  # pragma: no cover - host/runtime boundary
        error = error or f"connect_or_session {type(exc).__name__}: {exc}"
    return summarize_ws(
        endpoint,
        events,
        started,
        error=error,
        audio_bytes=len(pcm),
        sample_rate=sample_rate,
        committed=committed,
    )


def run_control_session(
    endpoint: str,
    pcm: bytes,
    sample_rate: int,
    action: str,
    *,
    first_timeout: float,
) -> dict[str, object]:
    """Exercise cancellation or disconnect without claiming a transcript."""

    started = time.perf_counter()
    events: list[dict[str, object]] = []
    error: str | None = None
    closed_without_commit = action == "disconnect"
    try:
        with sync_connect(
            endpoint,
            open_timeout=15,
            close_timeout=5,
            ping_interval=None,
            compression=None,
            proxy=None,
            max_size=None,
        ) as ws:
            recv_event(ws, events, started, first_timeout, "first_server_event")
            send_json(
                ws,
                events,
                started,
                {"type": "session.update", "session": {"sample_rate": sample_rate, "language": "zh-CN"}},
            )
            recv_event(ws, events, started, 3.0, "session_update_response")
            ws.send(pcm[: FRAME_BYTES * 5])
            events.append(
                {
                    "direction": "client",
                    "phase": "control_audio",
                    "offset_s": round(time.perf_counter() - started, 6),
                    "bytes": min(len(pcm), FRAME_BYTES * 5),
                }
            )
            if action == "cancel":
                send_json(ws, events, started, {"type": "response.cancel"})
                recv_event(ws, events, started, 2.0, "cancel_response")
            else:
                ws.close()
    except Exception as exc:  # pragma: no cover - host/runtime boundary
        error = f"{type(exc).__name__}: {exc}"
    return {
        "endpoint": endpoint,
        "action": action,
        "events": events,
        "event_types": [event_type(entry.get("event", {})) for entry in events if entry.get("direction") == "server"],
        "cancel_sent": action == "cancel",
        "closed_without_commit": closed_without_commit,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "error": error,
    }


def run_provider_queue_backpressure(
    endpoint: str,
    runtime: Path,
    model: Path,
    output: Path,
    sample_rate: int,
) -> dict[str, object]:
    """Saturate the real provider queue on a live native WebSocket session."""

    async def _run() -> dict[str, object]:
        from asr.config import AsrConfig
        from asr.errors import AsrError
        from asr.provider import NemotronProvider, NemotronStreamingSession

        parsed = urlsplit(endpoint)
        server_url = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        config = AsrConfig(
            provider="nemotron-speech-cpp",
            server_url=server_url,
            sample_rate=sample_rate,
            max_queue_chunks=1,
            backpressure_timeout_ms=0,
            runtime_root=output / "provider-runtime",
            model_root=output / "provider-models",
            cache_root=output / "provider-cache",
            runtime_executable=runtime,
            model_path=model,
        )
        session = await NemotronStreamingSession.connect(NemotronProvider(config))
        error_codes: list[str] = []
        try:
            frame = b"\x01\x00" * (FRAME_BYTES // 2)
            outcomes = await asyncio.gather(
                *(session.send_audio(frame) for _ in range(32)),
                return_exceptions=True,
            )
            error_codes.extend(
                error.code for error in outcomes if isinstance(error, AsrError)
            )
        finally:
            await session.cancel()
            await session.close()
        return {
            "endpoint": endpoint,
            "session_created": True,
            "provider_queue_saturated": "ASR_BACKPRESSURE" in error_codes,
            "error_codes": error_codes,
            "error": None,
        }

    started = time.perf_counter()
    try:
        result = asyncio.run(_run())
    except Exception as error:  # pragma: no cover - host/runtime boundary
        result = {
            "endpoint": endpoint,
            "session_created": False,
            "provider_queue_saturated": False,
            "error_codes": [],
            "error": f"{type(error).__name__}: {error}",
        }
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    result["scenario"] = "provider_queue_backpressure"
    return result


def run_provider_timestamp_session(
    endpoint: str,
    runtime: Path,
    model: Path,
    output: Path,
    pcm: bytes,
    sample_rate: int,
) -> dict[str, object]:
    """Collect the normalized provider events from one real native session."""

    async def _run() -> dict[str, object]:
        from asr.config import AsrConfig
        from asr.metrics import measure_events
        from asr.provider import NemotronProvider, NemotronStreamingSession

        parsed = urlsplit(endpoint)
        server_url = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        config = AsrConfig(
            provider="nemotron-speech-cpp",
            server_url=server_url,
            sample_rate=sample_rate,
            word_timestamps=True,
            runtime_root=output / "provider-timing-runtime",
            model_root=output / "provider-timing-models",
            cache_root=output / "provider-timing-cache",
            runtime_executable=runtime,
            model_path=model,
        )
        session = await NemotronStreamingSession.connect(NemotronProvider(config))
        events = []
        try:
            for offset in range(0, len(pcm), FRAME_BYTES):
                await session.send_audio(pcm[offset : offset + FRAME_BYTES])
            await session.commit()
            async for event in session.events():
                events.append(event)
                if event.type in {"final", "silence", "error", "disconnected"}:
                    break
        finally:
            await session.close()
        timing_events = [
            event
            for event in events
            if event.metadata.get("audio_timestamp_source") == "native_audio_processed"
        ]
        word_timestamps = [
            timestamp
            for event in events
            for timestamp in event.metadata.get("word_timestamps", [])
            if isinstance(timestamp, dict)
        ]
        return {
            "endpoint": endpoint,
            "events": [event.to_dict() for event in events],
            "metrics": measure_events(events),
            "native_timing": {
                "event_count": len(timing_events),
                "last_audio_ms": timing_events[-1].audio_ms if timing_events else None,
                "word_timestamp_count": len(word_timestamps),
            },
            "error": None,
        }

    started = time.perf_counter()
    try:
        result = asyncio.run(_run())
    except Exception as error:  # pragma: no cover - host/runtime boundary
        result = {
            "endpoint": endpoint,
            "events": [],
            "metrics": {},
            "native_timing": {"event_count": 0, "last_audio_ms": None, "word_timestamp_count": 0},
            "error": f"{type(error).__name__}: {error}",
        }
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


class VramSampler:
    def __init__(self) -> None:
        self.command = shutil.which("nvidia-smi")
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.samples: list[dict[str, object]] = []

    def start(self) -> None:
        if self.command:
            self.thread = threading.Thread(target=self._run, name="b05-vram", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        assert self.command is not None
        while not self.stop_event.is_set():
            try:
                output = subprocess.check_output(
                    [
                        self.command,
                        "--query-gpu=index,name,memory.used,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    text=True,
                    stderr=subprocess.STDOUT,
                    timeout=5,
                )
                for line in output.splitlines():
                    parts = [part.strip() for part in line.split(",")]
                    if len(parts) == 4:
                        self.samples.append(
                            {
                                "monotonic_s": time.perf_counter(),
                                "index": parts[0],
                                "name": parts[1],
                                "used_mib": float(parts[2]),
                                "total_mib": float(parts[3]),
                            }
                        )
            except Exception as error:  # pragma: no cover - host diagnostic boundary
                self.samples.append({"monotonic_s": time.perf_counter(), "error": str(error)})
            self.stop_event.wait(0.2)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def summary(self) -> dict[str, object]:
        valid = [sample for sample in self.samples if "used_mib" in sample]
        return {
            "available": bool(self.command),
            "command": self.command,
            "sample_count": len(valid),
            "peak_used_mib": max((float(sample["used_mib"]) for sample in valid), default=None),
            "peak_total_mib": max((float(sample["total_mib"]) for sample in valid), default=None),
            "samples": self.samples,
        }


def start_server(
    runtime: Path,
    model: Path,
    cuda_root: Path,
    port: int,
    server_threads: int,
    output: Path,
) -> tuple[subprocess.Popen[bytes], Any, Any]:
    output.mkdir(parents=True, exist_ok=True)
    stdout = (output / "runtime.stdout.log").open("wb")
    stderr = (output / "runtime.stderr.log").open("wb")
    environment = dict(os.environ)
    path_entries = [
        str(runtime.parent),
        str(cuda_root / "bin"),
        str(cuda_root / "bin" / "x64"),
        str(cuda_root / "nvvm" / "bin" / "x64"),
    ]
    environment["PATH"] = os.pathsep.join(path_entries + [environment.get("PATH", "")])
    environment["CUDA_PATH"] = str(cuda_root)
    arguments = [
        str(runtime),
        "serve",
        "--no-ui",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--threads",
        str(server_threads),
        "--asr-model",
        str(model),
        "--device",
        "cuda:0",
        "--no-warmup",
        "--access-log",
        "--log-format",
        "json",
    ]
    process = subprocess.Popen(
        arguments,
        cwd=str(runtime.parent),
        env=environment,
        stdout=stdout,
        stderr=stderr,
    )
    return process, stdout, stderr


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--cuda-root", type=Path, required=True)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".evidence/B05_STREAMING_ASR/native-live-v6"))
    parser.add_argument("--port", type=int, default=18086)
    parser.add_argument("--server-threads", type=int, default=4)
    parser.add_argument("--models-first", action="store_true", help="request /v1/models before the WS probes")
    parser.add_argument("--ready-timeout", type=float, default=90.0)
    parser.add_argument("--first-timeout", type=float, default=6.0)
    parser.add_argument("--final-timeout", type=float, default=20.0)
    parser.add_argument("--control-scenarios", action="store_true")
    parser.add_argument("--models-only", action="store_true", help="stop after the ordered /v1/models checks")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for name in ("runtime", "model", "cuda_root", "wav"):
        path = getattr(args, name)
        if not path.exists():
            raise SystemExit(f"missing {name}: {path}")
    pcm, sample_rate, channels, sample_width, compression = read_wav(args.wav)
    output = args.output.resolve()
    base_url = f"http://127.0.0.1:{args.port}"
    endpoints = (
        f"ws://127.0.0.1:{args.port}/v1/realtime",
        f"ws://127.0.0.1:{args.port}/v1/audio/transcriptions/realtime",
    )
    process, stdout, stderr = start_server(
        args.runtime, args.model, args.cuda_root, args.port, args.server_threads, output
    )
    sampler = VramSampler()
    sampler.start()
    result: dict[str, object]
    try:
        ready_observations = wait_ready(base_url, args.ready_timeout)
        ready = ready_observations[-1] if ready_observations else {}
        try:
            ready_body = json.loads(str(ready.get("body", "")))
        except json.JSONDecodeError:
            ready_body = {}
        health = http_call(base_url, "GET", "/health")
        models_before: dict[str, object] | None = None
        if args.models_first:
            models_before = http_call(base_url, "GET", "/v1/models")
        ws_results = [] if args.models_only else [
            run_sync_session(
                endpoint,
                pcm,
                sample_rate,
                first_timeout=args.first_timeout,
                final_timeout=args.final_timeout,
                pace_s=None,
            )
            for endpoint in endpoints
        ]
        controls: dict[str, object] = {}
        if args.control_scenarios:
            controls["cancel"] = run_control_session(
                endpoints[0], pcm, sample_rate, "cancel", first_timeout=args.first_timeout
            )
            controls["disconnect"] = run_control_session(
                endpoints[0], pcm, sample_rate, "disconnect", first_timeout=args.first_timeout
            )
            silence = run_sync_session(
                endpoints[0],
                b"\x00\x00" * (sample_rate * 2),
                sample_rate,
                first_timeout=args.first_timeout,
                final_timeout=args.final_timeout,
                pace_s=0.02,
            )
            silence["scenario"] = "silence"
            controls["silence"] = silence
            burst = run_sync_session(
                endpoints[0],
                pcm,
                sample_rate,
                first_timeout=args.first_timeout,
                final_timeout=args.final_timeout,
                pace_s=None,
            )
            burst["scenario"] = "burst"
            controls["burst"] = burst
            controls["provider_timestamps"] = run_provider_timestamp_session(
                endpoints[0], args.runtime, args.model, output, pcm, sample_rate
            )
            controls["backpressure"] = run_provider_queue_backpressure(
                endpoints[0], args.runtime, args.model, output, sample_rate
            )
        models_after = http_call(base_url, "GET", "/v1/models")
        speech = ws_results[0] if ws_results else {"final_text": "", "partial_texts": []}
        final_text = str(speech.get("final_text", ""))
        vram_summary = sampler.summary()
        gpu_names = sorted(
            {
                str(sample.get("name", ""))
                for sample in vram_summary.get("samples", [])
                if isinstance(sample, dict) and sample.get("name")
            }
        )
        model_digest = sha256(args.model)
        scoring = score(REFERENCE, final_text)
        candidate_reasons: list[str] = []
        if not args.models_only and not all(bool(item.get("session_created")) for item in ws_results):
            candidate_reasons.append("both WS endpoints must emit session.created")
        if not args.models_only and not speech.get("partial_texts"):
            candidate_reasons.append("speech must emit a non-empty partial")
        if not args.models_only and not final_text:
            candidate_reasons.append("speech must emit a non-empty final")
        if not args.models_only and any(
            item.get("native_timing", {}).get("audio_processed_event_count", 0) < 1
            or item.get("native_timing", {}).get("word_timestamp_count", 0) < 1
            for item in ws_results
        ):
            candidate_reasons.append("speech must expose native audio and word timestamps")
        if not any("\u4e00" <= character <= "\u9fff" for character in final_text):
            candidate_reasons.append("speech final must contain Chinese characters")
        if not isinstance(scoring.get("cer"), (int, float)) or scoring["cer"] > MAX_CER:
            candidate_reasons.append(f"CER must be <= {MAX_CER:.2f}")
        if not isinstance(scoring.get("wer"), (int, float)) or scoring["wer"] > MAX_WER:
            candidate_reasons.append(f"WER must be <= {MAX_WER:.2f}")
        if not args.models_only and not args.control_scenarios:
            candidate_reasons.append("control scenarios are required for a native acceptance candidate")
        if models_after.get("status") != 200:
            candidate_reasons.append("/v1/models must return HTTP 200")
        if model_digest != MODEL_SHA256:
            candidate_reasons.append("model SHA-256 must match the pinned B05 model")
        if not any("rtx 3080" in name.casefold() for name in gpu_names):
            candidate_reasons.append("nvidia-smi must observe the pinned RTX 3080 device")
        if args.control_scenarios:
            cancel = controls["cancel"]
            if cancel.get("error") or "input_audio_buffer.cleared" not in cancel.get("event_types", []):
                candidate_reasons.append("cancel must clear the input audio buffer without an error")
            disconnect = controls["disconnect"]
            if disconnect.get("error") or not disconnect.get("closed_without_commit"):
                candidate_reasons.append("disconnect must close before commit without an error")
            silence = controls["silence"]
            if (
                silence.get("error")
                or silence.get("completed_event_count", 0) < 1
                or silence.get("final_text")
                or silence.get("partial_texts")
            ):
                candidate_reasons.append("silence must complete with no partial or transcript")
            backpressure = controls["backpressure"]
            if backpressure.get("error") or not backpressure.get("provider_queue_saturated"):
                candidate_reasons.append("live provider queue saturation must emit ASR_BACKPRESSURE")
            provider_timestamps = controls["provider_timestamps"]
            native_timing = provider_timestamps.get("native_timing", {})
            if (
                provider_timestamps.get("error")
                or native_timing.get("event_count", 0) < 1
                or native_timing.get("word_timestamp_count", 0) < 1
            ):
                candidate_reasons.append("provider must expose native audio and word timestamps")
        control_summary = {
            "cancel": {
                "cleared": "input_audio_buffer.cleared" in controls.get("cancel", {}).get("event_types", []),
                "error": controls.get("cancel", {}).get("error"),
            },
            "disconnect": {
                "closed_without_commit": controls.get("disconnect", {}).get("closed_without_commit") is True,
                "error": controls.get("disconnect", {}).get("error"),
            },
            "silence": {
                "completed": controls.get("silence", {}).get("completed_event_count", 0) >= 1,
                "empty": not controls.get("silence", {}).get("final_text")
                and not controls.get("silence", {}).get("partial_texts"),
                "error": controls.get("silence", {}).get("error"),
            },
            "backpressure": {
                "provider_queue_saturated": controls.get("backpressure", {}).get("provider_queue_saturated") is True,
                "error_codes": controls.get("backpressure", {}).get("error_codes", []),
                "error": controls.get("backpressure", {}).get("error"),
            },
            "provider_timestamps": {
                "event_count": controls.get("provider_timestamps", {}).get("native_timing", {}).get("event_count", 0),
                "last_audio_ms": controls.get("provider_timestamps", {}).get("native_timing", {}).get("last_audio_ms"),
                "word_timestamp_count": controls.get("provider_timestamps", {}).get("native_timing", {}).get("word_timestamp_count", 0),
                "error": controls.get("provider_timestamps", {}).get("error"),
            },
        }
        result = {
            "status": "COMPLETE" if ready_body.get("ready") is True else "UNAVAILABLE",
            "native_acceptance_candidate": not candidate_reasons,
            "candidate_reasons": candidate_reasons,
            "request_order": {
                "models_first": args.models_first,
                "models_only": args.models_only,
                "sequence": ["ready", "health"]
                + (["v1/models"] if args.models_first else [])
                + ["ws_primary", "ws_alias", "v1/models_after"],
            },
            "runtime": {
                "exe": str(args.runtime),
                "model": str(args.model),
                "model_sha256": model_digest,
                "cuda_root": str(args.cuda_root),
                "pid": process.pid,
                "server_threads": args.server_threads,
            },
            "fixture": {
                "wav": str(args.wav),
                "sample_rate": sample_rate,
                "channels": channels,
                "sample_width_bytes": sample_width,
                "compression": compression,
                "duration_s": len(pcm) / 2 / sample_rate,
            },
            "reference": {
                "text": REFERENCE,
                "scoring": scoring,
            },
            "ready_observations": ready_observations,
            "ready": ready,
            "health": health,
            "models_before": models_before,
            "models_after": models_after,
            "ws": ws_results,
            "controls": controls,
            "vram": vram_summary,
            "process_exit_before_cleanup": process.poll(),
        }
        device_label = str(ready_body.get("device", ""))
        if gpu_names:
            device_label = f"{device_label} ({', '.join(gpu_names)})".strip()
        manifest_path = output / "evidence" / "native_acceptance.json"
        write_json(
            manifest_path,
            {
                "verified": result["native_acceptance_candidate"],
                "backend": "cuda",
                "device": device_label,
                "model_revision": MODEL_REVISION,
                "model_sha256": model_digest,
                "runtime_revision": RUNTIME_REVISION,
                "websocket_roundtrip": bool(
                    ws_results
                    and all(
                        item.get("session_created")
                        and item.get("partial_texts")
                        and item.get("final_text")
                        and not item.get("error")
                        for item in ws_results
                    )
                ),
                "native_timing": [item.get("native_timing") for item in ws_results],
                "models_before_status": models_before.get("status") if models_before else None,
                "models_after_status": models_after.get("status"),
                "cer": result["reference"]["scoring"]["cer"],
                "wer": result["reference"]["scoring"]["wer"],
                "ready_http_status": ready.get("status"),
                "health_status": health.get("status"),
                "peak_vram_mib": vram_summary["peak_used_mib"],
                "process_exit_before_cleanup": result["process_exit_before_cleanup"],
                "controls": control_summary,
                "request_order": result["request_order"],
                "probe_json": str(output / "probe.json"),
            },
        )
        result["acceptance_manifest"] = str(manifest_path)
        write_json(output / "probe.json", result)
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "models_first": args.models_first,
                    "ready": ready_body,
                    "models_before_status": models_before.get("status") if models_before else None,
                    "models_after_status": models_after.get("status"),
                    "ws": [
                        {
                            "endpoint": item["endpoint"],
                            "session_created": item["session_created"],
                            "partial_count": item["partial_count"],
                            "final_text": item["final_text"],
                            "first_partial_latency_ms": item["first_partial_latency_ms"],
                            "final_latency_ms": item["final_latency_ms"],
                            "error": item["error"],
                        }
                        for item in ws_results
                    ],
                    "candidate": result["native_acceptance_candidate"],
                    "candidate_reasons": candidate_reasons,
                    "peak_vram_mib": result["vram"]["peak_used_mib"],
                    "probe_json": str(output / "probe.json"),
                },
                ensure_ascii=False,
            )
        )
        return 0 if ready_body.get("ready") is True else 2
    finally:
        sampler.stop()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        stdout.close()
        stderr.close()


if __name__ == "__main__":
    raise SystemExit(main())
