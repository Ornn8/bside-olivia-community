"""Build a deterministic, rights-clean B05 audio contract fixture.

This tool intentionally produces a tone/silence fixture, not speech and not a
native-provider result.  Its manifest makes that boundary explicit so it can
be listened to without being mistaken for WER or GPU evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import wave
from pathlib import Path
from struct import pack

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.config import AsrConfig  # noqa: E402
from asr.contracts import EventClock  # noqa: E402
from asr.metrics import measure_events  # noqa: E402
from asr.provider import NemotronProvider  # noqa: E402


def _write_fixture(path: Path, *, sample_rate: int = 16_000) -> dict[str, object]:
    duration_seconds = 1.0
    sample_count = int(sample_rate * duration_seconds)
    tone_samples = int(sample_rate * 0.3)
    frames = bytearray()
    for index in range(sample_count):
        value = int(0.18 * 32767 * math.sin(2 * math.pi * 440 * index / sample_rate)) if index < tone_samples else 0
        frames.extend(pack("<h", value))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(frames)
    return {
        "path": str(path.absolute()),
        "sample_rate": sample_rate,
        "channels": 1,
        "sample_width_bytes": 2,
        "duration_seconds": duration_seconds,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source": "deterministic locally generated 440 Hz tone plus silence; no external media",
        "rights": "project-generated, non-copyright-restricted fixture",
    }


def build(output_root: Path, config: AsrConfig) -> dict[str, object]:
    output_root = output_root.absolute()
    audio = _write_fixture(output_root / "synthetic_tone_silence_16k.wav", sample_rate=config.sample_rate)
    clock = EventClock(session_id="b05-contract-fixture")
    events = [
        clock.emit("session", provider="offline-contract-fixture", metadata={"native_run": False}),
        clock.emit("ready", provider="offline-contract-fixture", metadata={"native_run": False}),
        clock.emit(
            "silence",
            provider="offline-contract-fixture",
            audio_ms=1000.0,
            metadata={"native_run": False, "fixture": "tone-plus-silence"},
        ),
        clock.emit("closed", provider="offline-contract-fixture", audio_ms=1000.0),
    ]
    event_payload = {
        "evidence_class": "offline-contract-fixture",
        "native_run": False,
        "native_status": NemotronProvider(config).status(),
        "events": [event.to_dict() for event in events],
    }
    event_path = output_root / "events.json"
    event_path.write_text(json.dumps(event_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    metrics = measure_events(events)
    metrics.update(
        {
            "evidence_class": "offline-contract-fixture",
            "reference_transcript": "",
            "hypothesis_transcript": "",
            "wer": None,
            "cer": None,
            "wer_note": "No speech is present; WER/CER are intentionally not reported.",
            "rtf": None,
        }
    )
    metrics_path = output_root / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "evidence_class": "offline-contract-fixture",
        "native_acceptance": False,
        "native_status": event_payload["native_status"],
        "audio": audio,
        "events": str(event_path.absolute()),
        "metrics": str(metrics_path.absolute()),
        "model_weights_included": False,
        "private_data_included": False,
        "secrets_included": False,
        "note": "Playable tone/silence only; never use this fixture to claim ASR WER, GPU, or WebSocket success.",
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a rights-clean B05 offline audio evidence fixture")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    config = AsrConfig.from_json(args.config) if args.config else AsrConfig(provider="nemotron-speech-cpp")
    manifest = build(args.output_root, config)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
