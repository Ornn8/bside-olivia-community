"""In-memory B07 compositor and conservative fallback behavior."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable, Mapping, Protocol

from .contracts import (
    DRIVEN,
    FALLBACK,
    PROTECTED_REGION_IDS,
    VisualDriverError,
    VisualDriverRequest,
    VisualDriverResult,
    _validate_frame,
    np,
    state_coverage_document,
)


class VisualBackend(Protocol):
    """A replaceable local backend; it returns an in-memory candidate frame."""

    def render(self, request: VisualDriverRequest) -> Any:
        """Render only the speaking region of an original frame."""


BackendLike = VisualBackend | Callable[[VisualDriverRequest], Any]
QualityGuard = Callable[[VisualDriverRequest, Any], bool]


def _mask(value: Any, shape: tuple[int, int], code: str) -> Any:
    if np is None:
        raise VisualDriverError("numpy_unavailable", retryable=True)
    try:
        array = np.asarray(value)
    except (TypeError, ValueError) as exc:
        raise VisualDriverError(code) from exc
    if array.ndim == 3:
        array = np.any(array != 0, axis=2)
    if array.ndim != 2 or tuple(array.shape) != shape:
        raise VisualDriverError(code)
    return array != 0


def _protected_mask(request: VisualDriverRequest, shape: tuple[int, int]) -> Any:
    if np is None:
        raise VisualDriverError("numpy_unavailable", retryable=True)
    protected = np.zeros(shape, dtype=bool)
    for region_id, value in request.protected_regions.items():
        if region_id not in PROTECTED_REGION_IDS:
            continue
        protected |= _mask(value, shape, "protected_region_mask_invalid")
    return protected


def _protected_region_copies(request: VisualDriverRequest, shape: tuple[int, int]) -> dict[str, Any]:
    """Give a backend normalized masks that cannot alias caller-owned data."""

    return {
        region_id: _mask(value, shape, "protected_region_mask_invalid").copy()
        for region_id, value in request.protected_regions.items()
    }


def _invoke_backend(backend: BackendLike, request: VisualDriverRequest) -> Any:
    renderer = getattr(backend, "render", None)
    if callable(renderer):
        return renderer(request)
    if callable(backend):
        return backend(request)
    raise VisualDriverError("backend_invalid", retryable=True)


class VisualDriver:
    """Drive a local speaking region while preserving original visual pixels."""

    def __init__(
        self,
        backend: BackendLike | None = None,
        *,
        quality_guard: QualityGuard | None = None,
    ) -> None:
        self._backend = backend
        self._quality_guard = quality_guard

    def coverage(self, available_states: Mapping[str, Any] | set[str] | tuple[str, ...] | list[str]) -> dict[str, Any]:
        return state_coverage_document(available_states)

    def release_turn(self, turn_id: str) -> None:
        release = getattr(self._backend, "release_turn", None)
        if callable(release):
            release(turn_id)

    def close(self) -> None:
        close = getattr(self._backend, "close", None)
        if callable(close):
            close()

    def render(self, request: VisualDriverRequest) -> VisualDriverResult:
        """Render or fall back to the exact original frame.

        Every failure after the original frame has been validated returns a
        copy of that frame.  No black frame, blank frame, or synthetic frame
        is treated as a successful fallback.
        """

        if not isinstance(request, VisualDriverRequest):
            raise VisualDriverError("request_invalid")
        original = request.original.frame
        _validate_frame(original)
        original_array = np.asarray(original)
        shape = (int(original_array.shape[0]), int(original_array.shape[1]))

        def fallback(reason: str, *, protected_count: int = 0) -> VisualDriverResult:
            return VisualDriverResult(
                status=FALLBACK,
                state_id=request.state_id,
                frame=original_array.copy(),
                fallback_reason=reason,
                active_pixel_count=0,
                protected_pixel_count=protected_count,
            )

        if self._backend is None:
            return fallback("driver_unavailable")
        if request.speaking_mask is None:
            return fallback("speaking_mask_missing")
        try:
            speaking = _mask(request.speaking_mask, shape, "speaking_mask_invalid")
            protected = _protected_mask(request, shape)
            protected_region_copies = _protected_region_copies(request, shape)
        except VisualDriverError as exc:
            return fallback(exc.code)
        active = speaking & ~protected
        active_count = int(np.count_nonzero(active))
        protected_count = int(np.count_nonzero(protected))
        if active_count == 0:
            return fallback("speaking_region_protected", protected_count=protected_count)

        try:
            # Backends are untrusted replaceable code.  They receive a deep
            # copy of the original frame, manifest proof and masks so an
            # in-place edit cannot mutate the caller's original input.
            backend_original = replace(
                request.original,
                frame=original_array.copy(),
                asset_manifest=deepcopy(dict(request.original.asset_manifest or {})),
                metadata=deepcopy(dict(request.original.metadata)),
            )
            backend_request = replace(
                request,
                original=backend_original,
                speaking_mask=speaking.copy(),
                protected_regions=protected_region_copies,
            )
            candidate = _invoke_backend(self._backend, backend_request)
            if isinstance(candidate, Mapping):
                candidate = candidate.get("frame")
            _validate_frame(candidate)
            candidate_array = np.asarray(candidate)
        except VisualDriverError as exc:
            return fallback(exc.code, protected_count=protected_count)
        except Exception:
            return fallback("backend_error", protected_count=protected_count)

        if candidate_array.shape != original_array.shape or candidate_array.dtype != original_array.dtype:
            return fallback("backend_output_invalid", protected_count=protected_count)
        composed = original_array.copy()
        composed[active] = candidate_array[active]
        # The assignment above is the only write into the returned frame.  A
        # second explicit invariant makes the protected-area guarantee clear
        # and catches future compositor edits.
        if not np.array_equal(composed[protected], original_array[protected]):
            return fallback("protected_region_changed", protected_count=protected_count)
        if self._quality_guard is not None:
            try:
                guard_result = self._quality_guard(request, composed.copy())
                valid_guard_result = isinstance(guard_result, (bool, np.bool_))
            except Exception:
                return fallback("quality_guard_error", protected_count=protected_count)
            if not valid_guard_result or not bool(guard_result):
                return fallback("quality_guard_failed", protected_count=protected_count)
        return VisualDriverResult(
            status=DRIVEN,
            state_id=request.state_id,
            frame=composed,
            fallback_reason=None,
            active_pixel_count=active_count,
            protected_pixel_count=protected_count,
            output_source="in_memory_original_composite",
        )
