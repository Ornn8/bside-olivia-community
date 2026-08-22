"""Offline B08 health contract and truthful-readiness gate."""

from __future__ import annotations

import json
import asyncio
import hashlib
import os
import re
import sys
import wave
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
TOOL_DIR = Path(__file__).resolve().parent
sys.path[:] = [
    entry
    for entry in sys.path
    if not entry or Path(entry).resolve() != TOOL_DIR
]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from live import LiveService
from live.contracts import LiveError
from live.environment import build_live_environment


_HEALTH_KEYS = {"status", "ready", "components", "network_called"}
_COMPONENT_KEYS = {"status", "ready", "provider", "fallback", "reason_code"}
_OPTIONAL_COMPONENT_KEYS = {"network_called", "source"}

_DEEPSEEK_DEFAULTS = {
    "OLIVIA_LLM_PROVIDER": "openai_compatible",
    "OLIVIA_LLM_BASE_URL": "https://api.deepseek.com/v1",
    "OLIVIA_LLM_MODEL": "deepseek-chat",
    "OLIVIA_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
    "OLIVIA_LLM_REQUIRES_API_KEY": "true",
    "OLIVIA_LLM_STREAM": "true",
}


class _OutputPathTts:
    """Thin adapter that uses B06's existing start(output_path) boundary."""

    def __init__(self, service: Any, output_path: Path) -> None:
        self._service = service
        self._output_path = output_path

    def health(self) -> Mapping[str, Any]:
        return self._service.health()

    async def start(self, request: Any) -> Any:
        return await self._service.start(request, output_path=str(self._output_path))

    def close(self) -> None:
        close = getattr(self._service, "close", None)
        if callable(close):
            close()


def _runtime_environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    values = dict(os.environ if environ is None else environ)
    for name, value in _DEEPSEEK_DEFAULTS.items():
        values.setdefault(name, value)
    return values


def _read_pcm16_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getcomptype() != "NONE":
            raise ValueError("VOICE_INPUT_REQUIRES_MONO_PCM16_WAV")
        sample_rate = int(audio.getframerate())
        frames = audio.readframes(audio.getnframes())
    if not frames:
        raise ValueError("VOICE_INPUT_EMPTY")
    return frames, sample_rate


def _redact_transcript(value: str) -> str:
    text = " ".join(str(value).split())
    text = re.sub(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", "<redacted-email>", text)
    text = re.sub(r"\b(?:sk|key|token)-[A-Za-z0-9._-]{12,}\b", "<redacted-token>", text)
    return text[:4000]


def _result_public(result: Any) -> dict[str, Any]:
    assistant_text = str(getattr(result, "text", "") or "")
    return {
        "turn_id": result.turn_id,
        "status": result.status,
        "text_source": result.text_source,
        "error_code": result.error_code,
        "retryable": result.retryable,
        "memory_status": result.memory_status,
        "tts_status": result.tts_status,
        "visual_status": result.visual_status,
        "audio_chunks": result.audio_chunks,
        "latency_ms": round(float(result.latency_ms), 3),
        "assistant_text_present": bool(assistant_text),
        "assistant_text_chars": len(assistant_text),
        "assistant_text_sha256": hashlib.sha256(assistant_text.encode("utf-8")).hexdigest()
        if assistant_text
        else None,
    }


def _gateway_network_call_count(gateway: Any) -> int:
    """Read provider-bound call count without inferring it from response text."""

    try:
        value = getattr(gateway, "network_call_count", 0)
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


async def run_voice_turn(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: str | Path | None = None,
    audio_path: str | Path | None = None,
    text: str | None = None,
    output_path: str | Path | None = None,
    owner_id: str = "local-cli",
    service: LiveService | None = None,
) -> dict[str, Any]:
    """Run one text or PCM16 audio turn through the existing Live boundary.

    The returned report contains redacted ASR text and event timestamps, but
    never includes provider response text, credentials, raw PCM, or legacy
    letter content.
    """

    if (audio_path is None) == (text is None):
        raise ValueError("VOICE_INPUT_REQUIRES_EXACTLY_ONE_AUDIO_OR_TEXT")

    root = Path(project_root).absolute() if project_root is not None else ROOT
    input_bytes: bytes | None = None
    input_rate: int | None = None
    if audio_path is not None:
        input_bytes, input_rate = _read_pcm16_wav(Path(audio_path).absolute())

    output = Path(output_path).absolute() if output_path is not None else None
    environment = None
    if service is None:
        environment = build_live_environment(
            environ=_runtime_environment(environ),
            project_root=root,
        )
        service = LiveService(
            gateway=environment.gateway,
            memory_port=environment.memory_port,
            persona_provider=environment.persona_provider,
            asr_provider=environment.asr_provider,
            tts_service=environment.tts_service,
            visual_driver=environment.visual_driver,
            visual_request=environment.visual_request,
        )
        service.environment = environment
    tts_service: Any = service.tts_service
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        tts_service = _OutputPathTts(tts_service, output)
        service.tts_service = tts_service
    network_calls_before = _gateway_network_call_count(service.gateway)
    health = service.health()
    session = await service.start_session(owner_id)
    input_error: str | None = None
    try:
        if text is not None:
            handle = await session.submit_text(text)
        else:
            assert input_bytes is not None and input_rate is not None
            asr_config = getattr(getattr(service, "environment", None), "asr_config", None)
            expected_rate = getattr(asr_config, "sample_rate", input_rate)
            if input_rate != expected_rate:
                raise ValueError("VOICE_INPUT_SAMPLE_RATE_MISMATCH")
            handle = await session.start_audio_turn()
            chunk_bytes = max(2, int(input_rate * 0.1) * 2)
            try:
                for offset in range(0, len(input_bytes), chunk_bytes):
                    await session.send_audio(handle.turn_id, input_bytes[offset : offset + chunk_bytes])
                await session.commit_audio(handle.turn_id)
            except LiveError as exc:
                input_error = exc.code
        result = await handle.wait()
        trace = session.trace()
        report_status = "UNAVAILABLE" if health["status"] == "UNAVAILABLE" else result.status.upper()
        network_calls_after = _gateway_network_call_count(service.gateway)
        return {
            "status": report_status,
            "health": health,
            "network_called": network_calls_after > network_calls_before,
            "input": {
                "kind": "audio" if audio_path is not None else "text",
                "sample_rate": input_rate,
                "bytes": len(input_bytes) if input_bytes is not None else None,
                "input_error": input_error,
            },
            "transcript": _redact_transcript(session.input_transcript)
            if session.input_transcript
            else None,
            "transcript_sha256": hashlib.sha256(session.input_transcript.encode("utf-8")).hexdigest()
            if session.input_transcript
            else None,
            "timestamps": [
                {
                    "event": item["event"],
                    "timestamp_ms": item["timestamp_ms"],
                    "state": item["state"],
                    "component": item["component"],
                    "status": item["status"],
                    "error_code": item["error_code"],
                    "text_present": item["text_present"],
                    "metadata": item["metadata"],
                }
                for item in trace
            ],
            "result": _result_public(result),
            "output_wav": str(output) if output is not None and output.is_file() else None,
        }
    finally:
        await service.stop()


def check() -> dict[str, object]:
    service = LiveService.from_environment(
        environ={
            "OLIVIA_LLM_PROVIDER": "openai_compatible",
            "OLIVIA_LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "OLIVIA_LLM_MODEL": "local-model",
            "OLIVIA_LLM_API_KEY_ENV": "B08_SYNTHETIC_KEY",
            "OLIVIA_LLM_REQUIRES_API_KEY": "true",
            "B08_SYNTHETIC_KEY": "synthetic-secret",
        }
    )
    health = service.health()
    if set(health) != _HEALTH_KEYS:
        raise AssertionError("B08 health has fields outside the strict public contract")
    if health["ready"] is not False or health["network_called"] is not False:
        raise AssertionError("B08 health promoted an unverified environment")
    components = health["components"]
    if not isinstance(components, dict) or set(components) != {"llm", "memory", "asr", "tts", "visual"}:
        raise AssertionError("B08 health component set is incomplete")
    for component in components.values():
        if not isinstance(component, dict):
            raise AssertionError("B08 health component is not an object")
        if not _COMPONENT_KEYS <= set(component) <= _COMPONENT_KEYS | _OPTIONAL_COMPONENT_KEYS:
            raise AssertionError("B08 health component violates its versioned contract")
    llm = components["llm"]
    if llm["status"] != "DEGRADED" or llm["ready"] is not False:
        raise AssertionError("unverified external LLM was reported ready")
    if llm["reason_code"] != "LLM_REACHABILITY_UNVERIFIED":
        raise AssertionError("external LLM reachability reason is not explicit")
    return {
        "status": "PASS",
        "health_status": health["status"],
        "ready": health["ready"],
        "network_called": health["network_called"],
        "llm_status": llm["status"],
        "llm_ready": llm["ready"],
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the offline Live health gate or one Live voice turn.")
    parser.add_argument("--audio", type=Path, help="absolute mono PCM16 WAV input")
    parser.add_argument("--text", help="text input for a local text-to-voice turn")
    parser.add_argument("--output-wav", type=Path, help="absolute WAV output path")
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    args = parser.parse_args(argv)
    if args.audio is None and args.text is None:
        print(json.dumps(check(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.audio is not None and args.text is not None:
        parser.error("use exactly one of --audio or --text")
    try:
        report = asyncio.run(
            run_voice_turn(
                audio_path=args.audio,
                text=args.text,
                output_path=args.output_wav,
            )
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "FAILED", "error_code": str(exc)}, ensure_ascii=False))
        return 2
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if args.report is not None:
        args.report.absolute().parent.mkdir(parents=True, exist_ok=True)
        args.report.absolute().write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
