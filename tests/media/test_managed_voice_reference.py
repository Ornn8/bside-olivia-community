from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import wave

import pytest

from runtime.media.managed_voice_reference import (
    ManagedVoiceReferenceError,
    resolve_managed_voice_reference,
    resolve_managed_voice_reference_transcript,
)


def _managed_reference(root: Path, *, frames: int = 1_600) -> tuple[Path, str]:
    reference = root / "capabilities/video/shared/linli-reference.wav"
    reference.parent.mkdir(parents=True)
    with wave.open(str(reference), "wb") as target:
        target.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        target.writeframes(b"\0\0" * frames)
    digest = hashlib.sha256(reference.read_bytes()).hexdigest()
    reference.with_suffix(".json").write_text(
        json.dumps(
            {
                "schema_version": "olivia.managed-voice-reference.v1",
                "path": reference.name,
                "size_bytes": reference.stat().st_size,
                "sha256": digest,
                "wave": {
                    "channels": 1,
                    "sample_width_bytes": 2,
                    "sample_rate_hz": 16_000,
                    "frame_count": frames,
                    "compression_type": "NONE",
                },
            }
        ),
        encoding="utf-8",
    )
    return reference, digest


def test_resolver_accepts_a_complete_managed_reference(tmp_path: Path) -> None:
    reference, digest = _managed_reference(tmp_path)

    assert resolve_managed_voice_reference(
        tmp_path, expected_sha256=digest
    ) == reference.absolute()


def test_resolver_reads_only_a_hash_locked_managed_transcript(tmp_path: Path) -> None:
    reference, _digest = _managed_reference(tmp_path)
    transcript = reference.with_suffix(".txt")
    transcript.write_text("synthetic exact transcript\n", encoding="utf-8")
    metadata = json.loads(reference.with_suffix(".json").read_text(encoding="utf-8"))
    metadata.update(
        schema_version="olivia.managed-voice-reference.v2",
        transcript={
            "path": transcript.name,
            "size_bytes": transcript.stat().st_size,
            "sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
        },
    )
    reference.with_suffix(".json").write_text(json.dumps(metadata), encoding="utf-8")

    assert resolve_managed_voice_reference_transcript(tmp_path) == (
        "synthetic exact transcript"
    )
    transcript.write_text("tampered", encoding="utf-8")
    with pytest.raises(
        ManagedVoiceReferenceError, match="VOICE_REFERENCE_TRANSCRIPT_INVALID"
    ):
        resolve_managed_voice_reference_transcript(tmp_path)


def test_resolver_rejects_a_junction_inside_the_managed_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _reference, digest = _managed_reference(outside)
    shared = tmp_path / "data/capabilities/video/shared"
    shared.parent.mkdir(parents=True)
    completed = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(shared),
            str(outside / "capabilities/video/shared"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("Windows junctions are unavailable")

    with pytest.raises(ManagedVoiceReferenceError, match="VOICE_REFERENCE_INVALID"):
        resolve_managed_voice_reference(tmp_path / "data", expected_sha256=digest)


def test_resolver_rejects_a_truncated_wave_payload(tmp_path: Path) -> None:
    reference, _digest = _managed_reference(tmp_path)
    reference.write_bytes(reference.read_bytes()[:46])
    digest = hashlib.sha256(reference.read_bytes()).hexdigest()
    sidecar = reference.with_suffix(".json")
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    metadata.update(size_bytes=reference.stat().st_size, sha256=digest)
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ManagedVoiceReferenceError, match="VOICE_REFERENCE_INVALID"):
        resolve_managed_voice_reference(tmp_path, expected_sha256=digest)
