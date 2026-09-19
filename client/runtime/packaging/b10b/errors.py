"""Stable, sanitized B10B lifecycle errors."""

from __future__ import annotations

from typing import Any


class B10BError(Exception):
    """An expected, user-actionable lifecycle failure."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code
