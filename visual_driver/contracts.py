"""Public B07 contracts for an original-visual-only driver.

The runtime receives a logical B01 asset reference and an already decoded
original frame.  It never accepts a source path, stores media, or treats a
generated replacement as a visual fallback.  The optional backend can only
change the explicitly supplied speaking mask; every other pixel is copied
from the original frame by :mod:`visual_driver.engine`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

try:
    from tools import asset_manifest
except ImportError:  # pragma: no cover - direct package diagnostic path.
    asset_manifest = None

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - diagnostic environments only.
    np = None


SCHEMA_VERSION = 1
ASSET_REF_RE = re.compile(r"^asset_[0-9a-f]{32}$")
VISUAL_STATE_IDS = (
    "day",
    "dusk",
    "night",
    "idle",
    "piano_performance",
    "letter_reply",
    "letter_reading",
    "live",
    "outfit_variants",
    "scene_transitions",
)
REGION_IDS = (
    "face",
    "hair",
    "clothing",
    "skin",
    "framing",
    "background",
    "lighting",
    "clarity",
)
# A caller may provide a face contour that excludes the explicit speaking
# mask, or a full contour when the safe choice is to fall back unchanged.
# Every supplied named region is protected by the compositor.
PROTECTED_REGION_IDS = (
    "face",
    "hair",
    "clothing",
    "skin",
    "framing",
    "background",
    "lighting",
    "clarity",
)

MEASURED = "MEASURED"
UNAVAILABLE = "UNAVAILABLE"
UNVERIFIED = "UNVERIFIED"
UNFROZEN = "UNFROZEN"

DRIVEN = "DRIVEN"
FALLBACK = "FALLBACK"
ORIGINAL_INPUT = "b01_original"
ORIGINAL_FRAME_FALLBACK = "original_frame"
AV_SYNC_REASON = "b05_b06_runtime_unavailable"


class VisualDriverError(Exception):
    """A short, privacy-safe B07 error code."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _finite(value: Any, *, minimum: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise VisualDriverError("invalid_number") from exc
    if not math.isfinite(parsed) or (minimum is not None and parsed < minimum):
        raise VisualDriverError("invalid_number")
    return parsed


def _validate_frame(value: Any) -> None:
    if np is None:
        raise VisualDriverError("numpy_unavailable", retryable=True)
    try:
        array = np.asarray(value)
    except (TypeError, ValueError) as exc:
        raise VisualDriverError("original_frame_invalid") from exc
    if array.ndim not in {2, 3} or array.size == 0:
        raise VisualDriverError("original_frame_invalid")
    if any(int(dimension) <= 0 for dimension in array.shape):
        raise VisualDriverError("original_frame_invalid")
    if array.ndim == 3 and int(array.shape[2]) not in {1, 3, 4}:
        raise VisualDriverError("original_frame_channels_invalid")
    if not np.issubdtype(array.dtype, np.number):
        raise VisualDriverError("original_frame_dtype_invalid")
    if np.issubdtype(array.dtype, np.inexact) and not bool(np.all(np.isfinite(array))):
        raise VisualDriverError("original_frame_nonfinite")


def _validate_manifest_reference(manifest: Any, asset_ref: str) -> None:
    if asset_manifest is None:
        raise VisualDriverError("asset_manifest_unavailable", retryable=True)
    if not isinstance(manifest, Mapping):
        raise VisualDriverError("original_manifest_required")
    try:
        report = asset_manifest.validate_manifest_document(dict(manifest))
    except Exception as exc:
        raise VisualDriverError("original_manifest_invalid") from exc
    if not report.ok:
        raise VisualDriverError("original_manifest_invalid")
    matches = [
        item
        for item in manifest.get("items", [])
        if isinstance(item, Mapping) and item.get("logical_id") == asset_ref
    ]
    if len(matches) != 1:
        raise VisualDriverError("original_manifest_reference_unknown")
    if matches[0].get("category") not in {"image", "video"}:
        raise VisualDriverError("original_manifest_asset_not_visual")


@dataclass(frozen=True)
class OriginalVisualFrame:
    """A decoded frame selected by a private B01 manifest reference."""

    state_id: str
    asset_ref: str
    frame: Any = field(repr=False, compare=False)
    frame_index: int = 0
    timestamp_seconds: float = 0.0
    frame_rate: float | None = None
    asset_manifest: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.state_id not in VISUAL_STATE_IDS:
            raise VisualDriverError("state_invalid")
        if not isinstance(self.asset_ref, str) or ASSET_REF_RE.fullmatch(self.asset_ref) is None:
            raise VisualDriverError("original_asset_reference_invalid")
        _validate_manifest_reference(self.asset_manifest, self.asset_ref)
        object.__setattr__(self, "asset_manifest", MappingProxyType(dict(self.asset_manifest or {})))
        if isinstance(self.frame_index, bool) or not isinstance(self.frame_index, int) or self.frame_index < 0:
            raise VisualDriverError("frame_index_invalid")
        timestamp = _finite(self.timestamp_seconds, minimum=0.0)
        object.__setattr__(self, "timestamp_seconds", timestamp)
        if self.frame_rate is not None:
            frame_rate = _finite(self.frame_rate, minimum=0.0)
            if frame_rate <= 0:
                raise VisualDriverError("frame_rate_invalid")
            object.__setattr__(self, "frame_rate", frame_rate)
        if not isinstance(self.metadata, Mapping):
            raise VisualDriverError("frame_metadata_invalid")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        _validate_frame(self.frame)

    def public_dict(self) -> dict[str, Any]:
        """Return path-free metadata suitable for runtime events and reports."""

        return {
            "schema_version": SCHEMA_VERSION,
            "source_kind": ORIGINAL_INPUT,
            "manifest_verified": True,
            "state_id": self.state_id,
            "frame_index": self.frame_index,
            "timestamp_seconds": self.timestamp_seconds,
            "frame_rate": self.frame_rate,
        }


@dataclass(frozen=True)
class VisualDriverRequest:
    """One local render request; all source pixels come from ``original``."""

    original: OriginalVisualFrame
    speaking_mask: Any | None = field(default=None, repr=False, compare=False)
    protected_regions: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    turn_id: str | None = None
    chunk_id: str | None = None
    audio_pcm16: bytes | None = field(default=None, repr=False, compare=False)
    sample_rate: int | None = None
    sample_count: int | None = None
    audio_start_seconds: float | None = None
    audio_end_seconds: float | None = None
    pts_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.original, OriginalVisualFrame):
            raise VisualDriverError("original_frame_required")
        if not isinstance(self.protected_regions, Mapping):
            raise VisualDriverError("protected_regions_invalid")
        unknown = set(self.protected_regions) - set(REGION_IDS)
        if unknown:
            raise VisualDriverError("protected_region_invalid")
        object.__setattr__(self, "protected_regions", MappingProxyType(dict(self.protected_regions)))
        timed = (
            self.turn_id,
            self.chunk_id,
            self.audio_pcm16,
            self.sample_rate,
            self.sample_count,
            self.audio_start_seconds,
            self.audio_end_seconds,
            self.pts_seconds,
        )
        if not any(value is not None for value in timed):
            return
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise VisualDriverError("visual_turn_id_invalid")
        if not isinstance(self.chunk_id, str) or not self.chunk_id.strip():
            raise VisualDriverError("visual_chunk_id_invalid")
        if not isinstance(self.audio_pcm16, bytes) or not self.audio_pcm16 or len(self.audio_pcm16) % 2:
            raise VisualDriverError("visual_audio_pcm_invalid")
        if isinstance(self.sample_rate, bool) or not isinstance(self.sample_rate, int) or self.sample_rate <= 0:
            raise VisualDriverError("visual_sample_rate_invalid")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count <= 0:
            raise VisualDriverError("visual_sample_count_invalid")
        if len(self.audio_pcm16) != self.sample_count * 2:
            raise VisualDriverError("visual_audio_pcm_length_invalid")
        start = _finite(self.audio_start_seconds, minimum=0.0)
        end = _finite(self.audio_end_seconds, minimum=0.0)
        pts = _finite(self.pts_seconds, minimum=0.0)
        if end < start or not start <= pts <= end:
            raise VisualDriverError("visual_audio_timing_invalid")
        object.__setattr__(self, "audio_start_seconds", start)
        object.__setattr__(self, "audio_end_seconds", end)
        object.__setattr__(self, "pts_seconds", pts)

    @property
    def state_id(self) -> str:
        return self.original.state_id


@dataclass(frozen=True)
class VisualDriverResult:
    """In-memory result; ``to_dict`` intentionally omits frame bytes."""

    status: str
    state_id: str
    frame: Any = field(repr=False, compare=False)
    fallback_reason: str | None = None
    active_pixel_count: int = 0
    protected_pixel_count: int = 0
    output_source: str = ORIGINAL_FRAME_FALLBACK

    def __post_init__(self) -> None:
        if self.status not in {DRIVEN, FALLBACK}:
            raise VisualDriverError("result_status_invalid")
        if self.fallback_reason is None and self.status == FALLBACK:
            raise VisualDriverError("fallback_reason_required")
        if self.active_pixel_count < 0 or self.protected_pixel_count < 0:
            raise VisualDriverError("result_counts_invalid")
        _validate_frame(self.frame)

    @property
    def used_fallback(self) -> bool:
        return self.status == FALLBACK

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "result_kind": "visual_driver_result",
            "status": self.status,
            "state_id": self.state_id,
            "output_source": self.output_source,
            "fallback": {
                "used": self.used_fallback,
                "reason": self.fallback_reason,
            },
            "active_pixel_count": self.active_pixel_count,
            "protected_pixel_count": self.protected_pixel_count,
            "media_written": False,
            "original_visual_policy": {
                "source_kind": ORIGINAL_INPUT,
                "static_regions_preserved": True,
                "replacement_media_generated": False,
                "replacement_media_committed": False,
            },
        }


def unavailable_av_sync() -> dict[str, Any]:
    """Return the stable truthful B05/B06 sync boundary.

    B07 does not infer audio timing from a visual frame and does not turn
    missing B05/B06 runtime metadata into a zero-offset success.
    """

    return {
        "metric": "av_sync",
        "status": UNAVAILABLE,
        "value": None,
        "threshold_status": UNFROZEN,
        "reason": AV_SYNC_REASON,
        "source": "b05_b06_contract",
    }


def state_coverage_document(available_states: Mapping[str, Any] | set[str] | tuple[str, ...] | list[str]) -> dict[str, Any]:
    """Build a deterministic path-free report for all B01 visual states."""

    if isinstance(available_states, Mapping):
        available = set()
        for state_id, value in available_states.items():
            if state_id not in VISUAL_STATE_IDS or value is None:
                raise VisualDriverError("state_input_invalid")
            if isinstance(value, OriginalVisualFrame) and value.state_id != state_id:
                raise VisualDriverError("state_input_invalid")
            if not isinstance(value, OriginalVisualFrame):
                raise VisualDriverError("state_input_invalid")
            available.add(state_id)
    else:
        available = set(available_states)
    unknown = available - set(VISUAL_STATE_IDS)
    if unknown:
        raise VisualDriverError("state_invalid")
    units = [
        {
            "state_id": state_id,
            "status": "ORIGINAL_INPUT" if state_id in available else "FALLBACK_ONLY",
            "fallback_source": ORIGINAL_FRAME_FALLBACK,
            "media_written": False,
        }
        for state_id in VISUAL_STATE_IDS
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "coverage_kind": "b07_visual_state_coverage",
        "state_units": units,
        "state_count": len(units),
        "available_state_count": len(available),
        "missing_state_ids": [unit["state_id"] for unit in units if unit["status"] == "FALLBACK_ONLY"],
        "original_visual_policy": {
            "source_kind": ORIGINAL_INPUT,
            "candidate_media_generated": False,
            "candidate_media_committed": False,
        },
    }
