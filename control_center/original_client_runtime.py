"""Composition-only bridge for the original Olivia companion settings surface.

The original client remains the sole user-facing shell.  This module does not
open a socket, create an application, render a browser page, or access storage.
It only combines the already-reviewed original-client HTTP contract with the
already-reviewed service adapter and mounts them on an existing aiohttp app.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import inspect
from typing import Iterable

from aiohttp import web


_BACKEND_CLASS = "OriginalClientCompanionServiceBackend"
_BACKEND_MODULES = (
    "original_client_companion_backend",
    "original_client_companion_service_backend",
    "original_client_companion_services",
)
_MOUNT_NAMES = (
    "mount_original_client_companion_api",
    "mount_original_companion_read_api",
    "mount_companion_read_api",
    "mount_companion_api",
)


class OriginalClientRuntimeCompositionError(RuntimeError):
    """Stable composition error without service or filesystem details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OriginalClientCompanionRuntime:
    """Objects mounted into one existing local application."""

    backend: object
    trusted_origins: tuple[str, ...]


def _load_backend_class() -> type:
    matches: list[type] = []
    for module_name in _BACKEND_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise OriginalClientRuntimeCompositionError(
                "ORIGINAL_COMPANION_BACKEND_IMPORT_FAILED"
            ) from exc
        candidate = getattr(module, _BACKEND_CLASS, None)
        if isinstance(candidate, type):
            matches.append(candidate)
    if len(matches) != 1:
        raise OriginalClientRuntimeCompositionError(
            "ORIGINAL_COMPANION_BACKEND_UNAVAILABLE"
        )
    return matches[0]


def _load_mount_function():
    try:
        module = importlib.import_module("original_client_companion_api")
    except ModuleNotFoundError as exc:
        raise OriginalClientRuntimeCompositionError(
            "ORIGINAL_COMPANION_API_UNAVAILABLE"
        ) from exc
    matches = [
        getattr(module, name)
        for name in _MOUNT_NAMES
        if callable(getattr(module, name, None))
    ]
    if len(matches) != 1:
        discovered = [
            value
            for name, value in vars(module).items()
            if name.startswith("mount_")
            and "companion" in name
            and callable(value)
        ]
        matches = discovered
    if len(matches) != 1:
        raise OriginalClientRuntimeCompositionError(
            "ORIGINAL_COMPANION_MOUNT_UNAVAILABLE"
        )
    return matches[0]


def _semantic_service(name: str, services: dict[str, object | None]) -> object | None:
    normalized = name.casefold()
    if "memory" in normalized:
        return services["memory"]
    if "candidate" in normalized:
        return services["candidates"]
    if "private" in normalized or "world" in normalized:
        return services["private_world"]
    raise OriginalClientRuntimeCompositionError(
        "ORIGINAL_COMPANION_BACKEND_SIGNATURE_UNSUPPORTED"
    )


def _build_backend(
    *,
    memory_service: object | None,
    private_world_service: object | None,
    candidate_service: object | None,
) -> object:
    backend_class = _load_backend_class()
    signature = inspect.signature(backend_class)
    services = {
        "memory": memory_service,
        "private_world": private_world_service,
        "candidates": candidate_service,
    }
    kwargs: dict[str, object | None] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        try:
            kwargs[name] = _semantic_service(name, services)
        except OriginalClientRuntimeCompositionError:
            if parameter.default is inspect.Parameter.empty:
                raise
    try:
        return backend_class(**kwargs)
    except (TypeError, ValueError) as exc:
        raise OriginalClientRuntimeCompositionError(
            "ORIGINAL_COMPANION_BACKEND_INVALID"
        ) from exc


def _mount(
    app: web.Application,
    backend: object,
    trusted_origins: tuple[str, ...],
) -> None:
    mount = _load_mount_function()
    signature = inspect.signature(mount)
    kwargs: dict[str, object] = {}
    positional: list[object] = []
    for name, parameter in signature.parameters.items():
        normalized = name.casefold()
        value: object
        if normalized in {"app", "application"}:
            value = app
        elif "backend" in normalized or "provider" in normalized:
            value = backend
        elif "origin" in normalized:
            value = trusted_origins
        elif parameter.default is not inspect.Parameter.empty:
            continue
        else:
            raise OriginalClientRuntimeCompositionError(
                "ORIGINAL_COMPANION_MOUNT_SIGNATURE_UNSUPPORTED"
            )
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            kwargs[name] = value
    try:
        mount(*positional, **kwargs)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise OriginalClientRuntimeCompositionError(
            "ORIGINAL_COMPANION_MOUNT_FAILED"
        ) from exc


def mount_original_client_companion_runtime(
    app: web.Application,
    *,
    memory_service: object | None = None,
    private_world_service: object | None = None,
    candidate_service: object | None = None,
    trusted_origins: Iterable[str] = (),
) -> OriginalClientCompanionRuntime:
    """Mount existing companion services on an existing loopback app.

    Passing ``None`` keeps a capability honestly disabled.  The supplied
    service objects remain the owners of reads, reduction, persistence, and
    candidate decisions; this function never reaches into their storage.
    """

    if not isinstance(app, web.Application):
        raise TypeError("an aiohttp application is required")
    origins = tuple(trusted_origins)
    if any(not isinstance(value, str) or not value for value in origins):
        raise ValueError("trusted origins must be non-empty strings")
    if len(set(origins)) != len(origins):
        raise ValueError("trusted origins must be unique")
    backend = _build_backend(
        memory_service=memory_service,
        private_world_service=private_world_service,
        candidate_service=candidate_service,
    )
    _mount(app, backend, origins)
    return OriginalClientCompanionRuntime(backend=backend, trusted_origins=origins)


__all__ = [
    "OriginalClientCompanionRuntime",
    "OriginalClientRuntimeCompositionError",
    "mount_original_client_companion_runtime",
]
