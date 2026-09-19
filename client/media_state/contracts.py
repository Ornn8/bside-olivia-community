"""B09 public data contracts.

The only asset selectors accepted by this module are runtime references from a
private B01 manifest.  No source path or resolved asset reference is included
in snapshots, operation results, or events.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


ASSET_REF_RE = re.compile(r"^asset_[0-9a-f]{32}$")
TRACK_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SCENE_KEY_RE = re.compile(r"^(day|dusk|night)/(idle|piano_performance)$")


class MediaStateError(Exception):
    """An actionable, privacy-safe error with no path or asset payload."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Action(_ValueEnum):
    PLAY = "play"
    PAUSE = "pause"
    STOP = "stop"
    SEEK = "seek"
    SWITCH_TRACK = "switch_track"
    SWITCH_STATE = "switch_state"
    RECOVER = "recover"


class AssetKind(_ValueEnum):
    AUDIO = "audio"
    VIDEO = "video"


class AssetStatus(_ValueEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    DEGRADED = "degraded"


class FallbackPolicy(_ValueEnum):
    ERROR = "error"
    USE_DECLARED_FALLBACK = "use_declared_fallback"
    SILENT = "silent"


class OperationStatus(_ValueEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NOOP = "NOOP"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class PerformanceMode(_ValueEnum):
    IDLE = "idle"
    PIANO_PERFORMANCE = "piano_performance"


class PlaybackStatus(_ValueEnum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"
    ERROR = "error"


class TimeOfDay(_ValueEnum):
    DAY = "day"
    DUSK = "dusk"
    NIGHT = "night"


def _mapping_copy(value: Mapping[str, str] | None) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise MediaStateError("INVALID_TRACK")
    copied = dict(value)
    for key, asset_ref in copied.items():
        if not isinstance(key, str) or SCENE_KEY_RE.fullmatch(key) is None:
            raise MediaStateError("INVALID_SCENE_KEY")
        if not isinstance(asset_ref, str) or ASSET_REF_RE.fullmatch(asset_ref) is None:
            raise MediaStateError("INVALID_ASSET_REFERENCE")
    return MappingProxyType(copied)


@dataclass(frozen=True)
class SceneState:
    time_of_day: TimeOfDay = TimeOfDay.DAY
    performance: PerformanceMode = PerformanceMode.IDLE

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "time_of_day", TimeOfDay(self.time_of_day))
            object.__setattr__(self, "performance", PerformanceMode(self.performance))
        except ValueError as exc:
            raise MediaStateError("INVALID_SCENE_STATE") from exc

    @property
    def key(self) -> str:
        return f"{self.time_of_day.value}/{self.performance.value}"

    def to_dict(self) -> dict[str, str]:
        return {
            "time_of_day": self.time_of_day.value,
            "performance": self.performance.value,
        }


@dataclass(frozen=True)
class TrackDefinition:
    """Runtime catalog entry assembled from a private B01 manifest."""

    track_id: str
    audio_asset_ref: str
    visual_asset_refs: Mapping[str, str] = field(default_factory=dict)
    fallback_audio_asset_ref: str | None = None
    fallback_visual_asset_refs: Mapping[str, str] = field(default_factory=dict)
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.track_id, str) or TRACK_ID_RE.fullmatch(self.track_id) is None:
            raise MediaStateError("INVALID_TRACK_ID")
        if not isinstance(self.audio_asset_ref, str) or ASSET_REF_RE.fullmatch(self.audio_asset_ref) is None:
            raise MediaStateError("INVALID_ASSET_REFERENCE")
        if self.fallback_audio_asset_ref is not None and (
            not isinstance(self.fallback_audio_asset_ref, str)
            or ASSET_REF_RE.fullmatch(self.fallback_audio_asset_ref) is None
        ):
            raise MediaStateError("INVALID_ASSET_REFERENCE")
        if self.duration_seconds is not None:
            try:
                duration = float(self.duration_seconds)
            except (TypeError, ValueError) as exc:
                raise MediaStateError("INVALID_DURATION") from exc
            if not math.isfinite(duration) or duration <= 0:
                raise MediaStateError("INVALID_DURATION")
            object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(self, "visual_asset_refs", _mapping_copy(self.visual_asset_refs))
        object.__setattr__(
            self,
            "fallback_visual_asset_refs",
            _mapping_copy(self.fallback_visual_asset_refs),
        )

    @classmethod
    def from_mapping(cls, record: Mapping[str, Any]) -> "TrackDefinition":
        if not isinstance(record, Mapping):
            raise MediaStateError("INVALID_TRACK")
        try:
            return cls(
                track_id=record["track_id"],
                audio_asset_ref=record["audio_asset_ref"],
                visual_asset_refs=record.get("visual_asset_refs", {}),
                fallback_audio_asset_ref=record.get("fallback_audio_asset_ref"),
                fallback_visual_asset_refs=record.get("fallback_visual_asset_refs", {}),
                duration_seconds=record.get("duration_seconds"),
            )
        except KeyError as exc:
            raise MediaStateError("INVALID_TRACK") from exc


class MusicCatalog:
    def __init__(self, tracks: list[TrackDefinition] | tuple[TrackDefinition, ...]) -> None:
        if not isinstance(tracks, (list, tuple)):
            raise MediaStateError("INVALID_CATALOG")
        entries: dict[str, TrackDefinition] = {}
        for track in tracks:
            if not isinstance(track, TrackDefinition) or track.track_id in entries:
                raise MediaStateError("INVALID_CATALOG")
            entries[track.track_id] = track
        self._tracks = MappingProxyType(entries)

    def get(self, track_id: str) -> TrackDefinition:
        try:
            return self._tracks[track_id]
        except KeyError as exc:
            raise MediaStateError("TRACK_NOT_FOUND") from exc

    def __len__(self) -> int:
        return len(self._tracks)


@dataclass(frozen=True)
class MediaCommand:
    action: Action
    track_id: str | None = None
    position_seconds: float | None = None
    time_of_day: TimeOfDay | None = None
    performance: PerformanceMode | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "action", Action(self.action))
            if self.time_of_day is not None:
                object.__setattr__(self, "time_of_day", TimeOfDay(self.time_of_day))
            if self.performance is not None:
                object.__setattr__(self, "performance", PerformanceMode(self.performance))
        except ValueError as exc:
            raise MediaStateError("INVALID_COMMAND") from exc

    @classmethod
    def play(cls, track_id: str | None = None) -> "MediaCommand":
        return cls(Action.PLAY, track_id=track_id)

    @classmethod
    def pause(cls) -> "MediaCommand":
        return cls(Action.PAUSE)

    @classmethod
    def stop(cls) -> "MediaCommand":
        return cls(Action.STOP)

    @classmethod
    def seek(cls, position_seconds: float) -> "MediaCommand":
        return cls(Action.SEEK, position_seconds=position_seconds)

    @classmethod
    def switch_track(cls, track_id: str) -> "MediaCommand":
        return cls(Action.SWITCH_TRACK, track_id=track_id)

    @classmethod
    def switch_state(
        cls,
        *,
        time_of_day: TimeOfDay | None = None,
        performance: PerformanceMode | None = None,
    ) -> "MediaCommand":
        return cls(
            Action.SWITCH_STATE,
            time_of_day=time_of_day,
            performance=performance,
        )

    @classmethod
    def recover(cls) -> "MediaCommand":
        return cls(Action.RECOVER)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action.value}
        if self.track_id is not None:
            payload["track_id"] = self.track_id
        if self.position_seconds is not None:
            payload["position_seconds"] = self.position_seconds
        if self.time_of_day is not None:
            payload["time_of_day"] = self.time_of_day.value
        if self.performance is not None:
            payload["performance"] = self.performance.value
        return payload


@dataclass(frozen=True)
class MediaSnapshot:
    revision: int = 0
    playback: PlaybackStatus = PlaybackStatus.STOPPED
    track_id: str | None = None
    position_seconds: float = 0.0
    time_of_day: TimeOfDay = TimeOfDay.DAY
    performance: PerformanceMode = PerformanceMode.IDLE
    asset_status: AssetStatus = AssetStatus.UNKNOWN
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "playback", PlaybackStatus(self.playback))
            object.__setattr__(self, "time_of_day", TimeOfDay(self.time_of_day))
            object.__setattr__(self, "performance", PerformanceMode(self.performance))
            object.__setattr__(self, "asset_status", AssetStatus(self.asset_status))
        except ValueError as exc:
            raise MediaStateError("INVALID_SNAPSHOT") from exc
        if self.revision < 0 or not isinstance(self.revision, int):
            raise MediaStateError("INVALID_SNAPSHOT")
        try:
            position = float(self.position_seconds)
        except (TypeError, ValueError) as exc:
            raise MediaStateError("INVALID_SNAPSHOT") from exc
        if not math.isfinite(position) or position < 0:
            raise MediaStateError("INVALID_SNAPSHOT")
        object.__setattr__(self, "position_seconds", position)

    @property
    def scene(self) -> SceneState:
        return SceneState(self.time_of_day, self.performance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "playback": self.playback.value,
            "track_id": self.track_id,
            "position_seconds": self.position_seconds,
            "time_of_day": self.time_of_day.value,
            "performance": self.performance.value,
            "asset_status": self.asset_status.value,
            "last_error_code": self.last_error_code,
        }


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    action: Action
    status: OperationStatus
    snapshot: MediaSnapshot
    error_code: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "action": self.action.value,
            "status": self.status.value,
            "snapshot": self.snapshot.to_dict(),
            "error_code": self.error_code,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class MediaEvent:
    event_type: str
    operation_id: str
    action: Action
    status: OperationStatus
    snapshot: MediaSnapshot
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "operation_id": self.operation_id,
            "action": self.action.value,
            "status": self.status.value,
            "snapshot": self.snapshot.to_dict(),
            "error_code": self.error_code,
        }


def contract_document() -> dict[str, Any]:
    """Return the committed, path-free B09 contract document."""

    return {
        "schema_version": 1,
        "contract_version": "b09.v1",
        "asset_source": "b01.private_asset_manifest",
        "actions": [action.value for action in Action],
        "playback_states": [state.value for state in PlaybackStatus],
        "scene": {
            "time_of_day": [state.value for state in TimeOfDay],
            "performance": [state.value for state in PerformanceMode],
        },
        "operation_states": [state.value for state in OperationStatus],
        "fallback_policies": [policy.value for policy in FallbackPolicy],
        "error_codes": [
            "INVALID_COMMAND",
            "INVALID_TRACK_ID",
            "TRACK_REQUIRED",
            "TRACK_NOT_FOUND",
            "SEEK_OUT_OF_RANGE",
            "ASSET_RESOLVER_UNAVAILABLE",
            "ASSET_NOT_FOUND",
            "ASSET_CATEGORY_MISMATCH",
            "ASSET_MISSING",
            "ASSET_HASH_MISMATCH",
            "ASSET_INVALID_MEDIA",
            "ASSET_FALLBACK_UNAVAILABLE",
            "PLAYBACK_PROVIDER_UNAVAILABLE",
            "PROVIDER_ERROR",
            "REQUEST_ID_REUSED",
            "RETRY_NOT_AVAILABLE",
        ],
        "privacy": {
            "source_paths_in_events": False,
            "asset_references_in_events": False,
            "original_media_in_repository": False,
            "network_fallback": False,
        },
        "cancellation": {
            "state_commit": "after_provider_success",
            "provider_requirement": "operations_must_be_cancellation_safe",
        },
    }
