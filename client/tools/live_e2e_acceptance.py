"""Sanitized end-to-end acceptance entry for the existing Live public API.

This tool only composes ``LiveService`` and ``LiveSession``.  It does not
construct a provider, model, agent, or media engine.  Network use is opt-in at
the CLI boundary; injected services remain useful for offline contract tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path
from typing import Any, Mapping

from live import LiveError, LiveService


SCHEMA_VERSION = 1


def _network_calls(gateway: Any) -> int:
    try:
        return max(0, int(getattr(gateway, "network_call_count", 0)))
    except (TypeError, ValueError):
        return 0


def _service_declares_offline(service: LiveService) -> bool:
    """Accept only an explicitly offline injected gateway without opt-in."""

    gateway = service.gateway
    if getattr(gateway, "acceptance_offline_test_only", False) is True:
        return True
    config = getattr(gateway, "config", None)
    return getattr(config, "provider", None) == "mock"


def _read_pcm16_mono(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getcomptype() != "NONE":
            raise ValueError("VOICE_INPUT_REQUIRES_MONO_PCM16_WAV")
        sample_rate = int(audio.getframerate())
        payload = audio.readframes(audio.getnframes())
    if not payload:
        raise ValueError("VOICE_INPUT_EMPTY")
    return payload, sample_rate


def _event_time(events: list[Mapping[str, Any]], event: str) -> float | None:
    for item in events:
        if item.get("event") == event:
            try:
                return round(float(item["timestamp_ms"]), 3)
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _elapsed(later: float | None, earlier: float | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round(max(0.0, later - earlier), 3)


def summarize_timeline(
    timeline: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    *,
    wall_elapsed_ms: float,
) -> dict[str, float | None]:
    """Return timing-only facts from the redacted public replay trace."""

    events = [dict(item) for item in timeline]
    turn_started = _event_time(events, "turn_started")
    asr_final = _event_time(events, "asr_final")
    llm_started = _event_time(events, "llm_started")
    first_delta = _event_time(events, "llm_delta")
    llm_completed = _event_time(events, "llm_completed")
    tts_started = _event_time(events, "tts_started")
    first_chunk = _event_time(events, "audio_chunk")
    first_visual = _event_time(events, "visual_frame")
    terminal = next(
        (
            _event_time(events, name)
            for name in (
                "turn_completed",
                "turn_degraded",
                "turn_timeout",
                "turn_failed",
                "turn_cancelled",
                "turn_interrupted",
            )
            if _event_time(events, name) is not None
        ),
        None,
    )
    return {
        "asr_final_at_ms": asr_final,
        "llm_first_delta_after_start_ms": _elapsed(first_delta, llm_started),
        "llm_completed_after_start_ms": _elapsed(llm_completed, llm_started),
        "tts_first_chunk_after_start_ms": _elapsed(first_chunk, tts_started),
        "visual_first_frame_after_chunk_ms": _elapsed(first_visual, first_chunk),
        "turn_terminal_after_start_ms": _elapsed(terminal, turn_started),
        "e2e_wall_ms": round(max(0.0, wall_elapsed_ms), 3),
    }


def _result_public(result: Any) -> dict[str, Any]:
    return {
        "status": str(getattr(result, "status", "failed")).upper(),
        "text_source": str(getattr(result, "text_source", "none")),
        "error_code": getattr(result, "error_code", None),
        "retryable": bool(getattr(result, "retryable", False)),
        "memory_status": str(getattr(result, "memory_status", "session-only")),
        "tts_status": str(getattr(result, "tts_status", "not_started")),
        "visual_status": str(getattr(result, "visual_status", "not_started")),
        "audio_chunks": int(getattr(result, "audio_chunks", 0)),
        "visual_frames": int(getattr(result, "visual_frames", 0)),
        "latency_ms": round(max(0.0, float(getattr(result, "latency_ms", 0.0))), 3),
    }


async def run_live_acceptance(
    *,
    text: str | None = None,
    audio_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    project_root: str | Path | None = None,
    service: LiveService | None = None,
    allow_network: bool = False,
    cancel_after_ms: float | None = None,
) -> dict[str, Any]:
    """Run one public Live turn and return a timeline-only, redacted report."""

    if (text is None) == (audio_path is None):
        return _unavailable_report("VOICE_INPUT_REQUIRES_EXACTLY_ONE_INPUT")
    if not allow_network:
        if service is None or not _service_declares_offline(service):
            return _unavailable_report("NETWORK_NOT_ALLOWED")

    audio_payload: bytes | None = None
    sample_rate: int | None = None
    input_kind = "text" if text is not None else "audio"
    try:
        if audio_path is not None:
            audio_payload, sample_rate = _read_pcm16_mono(Path(audio_path).absolute())
        active_service = service or LiveService.from_environment(
            environ=dict(environ or {}),
            project_root=str(project_root) if project_root is not None else None,
        )
        before_calls = _network_calls(active_service.gateway)
        started = time.monotonic()
        session = await active_service.start_session("local-e2e-acceptance")
        if text is not None:
            handle = await session.submit_text(text)
        else:
            handle = await session.start_audio_turn()
            assert audio_payload is not None and sample_rate is not None
            expected_rate = getattr(getattr(active_service.environment, "asr_config", None), "sample_rate", sample_rate)
            if sample_rate != expected_rate:
                raise ValueError("VOICE_INPUT_SAMPLE_RATE_MISMATCH")
            chunk_bytes = max(2, int(sample_rate * 0.1) * 2)
            for offset in range(0, len(audio_payload), chunk_bytes):
                await session.send_audio(handle.turn_id, audio_payload[offset : offset + chunk_bytes])
            await session.commit_audio(handle.turn_id)

        cancellation = {"requested": cancel_after_ms is not None, "accepted": False}
        cancel_task: asyncio.Task[None] | None = None
        if cancel_after_ms is not None:
            async def cancel_later() -> None:
                await asyncio.sleep(max(0.0, float(cancel_after_ms)) / 1000.0)
                cancellation["accepted"] = await session.cancel_turn(handle.turn_id)

            cancel_task = asyncio.create_task(cancel_later())
        try:
            result = await handle.wait()
        finally:
            if cancel_task is not None:
                if not cancel_task.done():
                    cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass
        timeline = session.trace()
        wall_elapsed = (time.monotonic() - started) * 1000.0
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": str(getattr(result, "status", "failed")).upper(),
            "network_called": _network_calls(active_service.gateway) > before_calls,
            "input": {"kind": input_kind},
            "cancellation": cancellation,
            "result": _result_public(result),
            "timeline": [dict(item) for item in timeline],
            "metrics": summarize_timeline(timeline, wall_elapsed_ms=wall_elapsed),
        }
        return report
    except (LiveError, OSError, ValueError) as exc:
        return _unavailable_report(str(getattr(exc, "code", exc)), input_kind=input_kind)
    finally:
        if service is not None:
            await service.stop()
        elif "active_service" in locals():
            await active_service.stop()


def _unavailable_report(code: str, *, input_kind: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "UNAVAILABLE",
        "network_called": False,
        "input": {"kind": input_kind} if input_kind else {"kind": "none"},
        "cancellation": {"requested": False, "accepted": False},
        "result": {
            "status": "UNAVAILABLE",
            "text_source": "none",
            "error_code": code,
            "retryable": False,
            "memory_status": "session-only",
            "tts_status": "not_started",
            "visual_status": "not_started",
            "audio_chunks": 0,
            "visual_frames": 0,
            "latency_ms": 0.0,
        },
        "timeline": [],
        "metrics": {
            "asr_final_at_ms": None,
            "llm_first_delta_after_start_ms": None,
            "llm_completed_after_start_ms": None,
            "tts_first_chunk_after_start_ms": None,
            "visual_first_frame_after_chunk_ms": None,
            "turn_terminal_after_start_ms": None,
            "e2e_wall_ms": 0.0,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one sanitized Live E2E acceptance turn.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="one Live text turn; never copied to output")
    source.add_argument("--audio", type=Path, help="mono PCM16 WAV; never copied to output")
    parser.add_argument("--report", type=Path, help="write sanitized JSON report")
    parser.add_argument("--allow-network", action="store_true", help="permit the configured external LLM turn")
    parser.add_argument("--cancel-after-ms", type=float, help="request public cancellation after this delay")
    args = parser.parse_args(argv)
    report = asyncio.run(
        run_live_acceptance(
            text=args.text,
            audio_path=args.audio,
            allow_network=args.allow_network,
            cancel_after_ms=args.cancel_after_ms,
        )
    )
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if args.report is not None:
        args.report.absolute().parent.mkdir(parents=True, exist_ok=True)
        args.report.absolute().write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
