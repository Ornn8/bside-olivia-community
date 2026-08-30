"""Resolve the private voice reference installed beside the local runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import wave


MANAGED_VOICE_REFERENCE_RELATIVE_PATH = Path(
    "capabilities/video/shared/linli-reference.wav"
)
MANAGED_VOICE_REFERENCE_SHA256 = (
    "7bd846a55265d5ceb4dcf0ef164dc954066b8b056ac1e40d554b1e41d844a5bf"
)
MANAGED_VOICE_REFERENCE_WAVE = {
    "channels": 1,
    "sample_width_bytes": 2,
    "sample_rate_hz": 16_000,
    "frame_count": 77_600,
    "compression_type": "NONE",
}
_METADATA_SCHEMA = "olivia.managed-voice-reference.v1"


class ManagedVoiceReferenceError(RuntimeError):
    """Stable failure raised when an installed reference is not trustworthy."""


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ManagedVoiceReferenceError("VOICE_REFERENCE_INVALID") from exc
    return path.is_symlink() or bool(attributes & 0x0400)


def _reject_reparse_points(root: Path, reference: Path) -> None:
    candidate = root
    for part in MANAGED_VOICE_REFERENCE_RELATIVE_PATH.parts[:-1]:
        if _is_reparse_point(candidate):
            raise ManagedVoiceReferenceError("VOICE_REFERENCE_INVALID")
        candidate /= part
    if any(
        _is_reparse_point(path)
        for path in (candidate, reference, reference.with_suffix(".json"))
    ):
        raise ManagedVoiceReferenceError("VOICE_REFERENCE_INVALID")


def _wave_facts(path: Path) -> dict[str, int | str]:
    try:
        with wave.open(str(path), "rb") as source:
            facts: dict[str, int | str] = {
                "channels": source.getnchannels(),
                "sample_width_bytes": source.getsampwidth(),
                "sample_rate_hz": source.getframerate(),
                "frame_count": source.getnframes(),
                "compression_type": source.getcomptype(),
            }
            frames = source.readframes(int(facts["frame_count"]))
    except (EOFError, OSError, wave.Error) as exc:
        raise ManagedVoiceReferenceError("VOICE_REFERENCE_INVALID") from exc
    if any(facts[key] != value for key, value in MANAGED_VOICE_REFERENCE_WAVE.items() if key != "frame_count"):
        raise ManagedVoiceReferenceError("VOICE_REFERENCE_INVALID")
    expected_bytes = (
        int(facts["frame_count"])
        * int(facts["channels"])
        * int(facts["sample_width_bytes"])
    )
    if expected_bytes <= 0 or len(frames) != expected_bytes:
        raise ManagedVoiceReferenceError("VOICE_REFERENCE_INVALID")
    return facts


def resolve_managed_voice_reference(
    data_root: Path,
    *,
    expected_sha256: str = MANAGED_VOICE_REFERENCE_SHA256,
) -> Path:
    """Return the installed WAV only when its sidecar and bytes agree."""

    root = Path(data_root).absolute()
    reference = root / MANAGED_VOICE_REFERENCE_RELATIVE_PATH
    _reject_reparse_points(root, reference)
    try:
        metadata = json.loads(reference.with_suffix(".json").read_text(encoding="utf-8"))
        payload = reference.read_bytes()
        facts = _wave_facts(reference)
    except FileNotFoundError as exc:
        raise ManagedVoiceReferenceError("VOICE_REFERENCE_UNAVAILABLE") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedVoiceReferenceError("VOICE_REFERENCE_INVALID") from exc
    digest = hashlib.sha256(payload).hexdigest()
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"schema_version", "path", "size_bytes", "sha256", "wave"}
        or metadata.get("schema_version") != _METADATA_SCHEMA
        or metadata.get("path") != reference.name
        or metadata.get("size_bytes") != len(payload)
        or metadata.get("sha256") != digest
        or metadata.get("wave") != facts
        or digest != expected_sha256
    ):
        raise ManagedVoiceReferenceError("VOICE_REFERENCE_INVALID")
    if expected_sha256 == MANAGED_VOICE_REFERENCE_SHA256 and facts != MANAGED_VOICE_REFERENCE_WAVE:
        raise ManagedVoiceReferenceError("VOICE_REFERENCE_INVALID")
    return reference


__all__ = [
    "MANAGED_VOICE_REFERENCE_RELATIVE_PATH",
    "MANAGED_VOICE_REFERENCE_SHA256",
    "MANAGED_VOICE_REFERENCE_WAVE",
    "ManagedVoiceReferenceError",
    "resolve_managed_voice_reference",
]
