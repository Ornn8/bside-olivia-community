"""Stable, user-visible errors for the B05 ASR boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_CODES = {
    "ASR_CONFIG_INVALID",
    "ASR_DEPENDENCY_MISSING",
    "ASR_RUNTIME_MISSING",
    "ASR_MODEL_MISSING",
    "ASR_MODEL_CORRUPT",
    "ASR_PROVIDER_UNAVAILABLE",
    "ASR_NOT_PROBED",
    "ASR_NOT_READY",
    "ASR_DISCONNECTED",
    "ASR_CANCELED",
    "ASR_SILENCE",
    "ASR_BACKPRESSURE",
    "ASR_INVALID_AUDIO",
    "ASR_PROTOCOL_ERROR",
    "ASR_FINAL_TIMEOUT",
    "ASR_LANGUAGE_UNSUPPORTED",
    "ASR_TOOLCHAIN_MISSING",
    "ASR_TOOLCHAIN_CORRUPT",
    "ASR_TOOLCHAIN_INVALID",
}


@dataclass
class AsrError(Exception):
    """An error that can be safely represented by an ASR error event."""

    code: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown ASR error code: {self.code}")
        Exception.__init__(self, f"{self.code}: {self.reason}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "reason": self.reason, "details": dict(self.details)}
