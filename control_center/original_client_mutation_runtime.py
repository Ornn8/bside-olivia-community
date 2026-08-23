"""Compose original-client mutations from services already mounted for reads.

The module walks a small, bounded process-local object graph to locate the
existing auditable Memory Admin and typed candidate-decision services.  It does
not construct providers, open storage, create an application, or start a
listener.  The original Olivia client remains the only user-facing shell.
"""

from __future__ import annotations

from collections import deque
from dataclasses import fields, is_dataclass
import inspect
from types import ModuleType
from typing import Iterable, Mapping

from aiohttp import web

from control_center.original_client_mutation_services import (
    OriginalClientCompanionMutationServiceBackend,
)
from original_client_companion_mutation_api import (
    mount_original_client_companion_mutation_api,
)


_MEMORY_CORRECT = frozenset(
    {"correct_memory", "correct", "replace_memory", "replace"}
)
_MEMORY_DELETE = frozenset(
    {"delete_memory", "delete", "remove_memory", "remove"}
)
_CANDIDATE_DECIDE = frozenset(
    {"decide_candidate", "decide", "record_decision"}
)
_MAX_GRAPH_NODES = 256
_MAX_GRAPH_DEPTH = 5


class OriginalClientMutationRuntimeError(RuntimeError):
    """Stable composition failure without storage or provider details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _callable_names(value: object) -> frozenset[str]:
    names: set[str] = set()
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            candidate = getattr(value, name)
        except Exception:
            continue
        if callable(candidate):
            names.add(name)
    return frozenset(names)


def _is_memory_service(value: object) -> bool:
    names = _callable_names(value)
    return bool(names & _MEMORY_CORRECT) and bool(names & _MEMORY_DELETE)


def _is_candidate_service(value: object) -> bool:
    names = _callable_names(value)
    return bool(names & _CANDIDATE_DECIDE)


def _children(value: object) -> tuple[object, ...]:
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, (tuple, list, set, frozenset, deque)):
        return tuple(value)
    if is_dataclass(value) and not isinstance(value, type):
        children: list[object] = []
        for field in fields(value):
            try:
                children.append(getattr(value, field.name))
            except Exception:
                continue
        return tuple(children)
    namespace = getattr(value, "__dict__", None)
    if isinstance(namespace, dict):
        return tuple(
            item
            for name, item in namespace.items()
            if not name.startswith("__")
        )
    return ()


def _walk(roots: Iterable[object]) -> tuple[object, ...]:
    queue = deque((value, 0) for value in roots)
    seen: set[int] = set()
    values: list[object] = []
    while queue:
        value, depth = queue.popleft()
        if value is None or isinstance(
            value,
            (str, bytes, bytearray, int, float, bool, ModuleType, type),
        ):
            continue
        if inspect.isroutine(value):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        values.append(value)
        if len(values) > _MAX_GRAPH_NODES:
            raise OriginalClientMutationRuntimeError(
                "ORIGINAL_COMPANION_RUNTIME_GRAPH_TOO_LARGE"
            )
        if depth >= _MAX_GRAPH_DEPTH:
            continue
        for child in _children(value):
            queue.append((child, depth + 1))
    return tuple(values)


def _unique_service(
    values: tuple[object, ...],
    predicate,
    *,
    code: str,
) -> object | None:
    matches = [value for value in values if predicate(value)]
    identities = {id(value) for value in matches}
    if len(identities) > 1:
        raise OriginalClientMutationRuntimeError(code)
    return matches[0] if matches else None


def mount_original_client_companion_mutations_from_app(
    app: web.Application,
    *,
    trusted_origins: tuple[str, ...] = (),
    extra_roots: Iterable[object] = (),
) -> OriginalClientCompanionMutationServiceBackend:
    """Discover existing services and mount mutations on the same application."""

    if not isinstance(app, web.Application):
        raise TypeError("an aiohttp application is required")
    roots = tuple(app.values()) + tuple(extra_roots)
    values = _walk(roots)
    memory_service = _unique_service(
        values,
        _is_memory_service,
        code="ORIGINAL_COMPANION_MEMORY_SERVICE_AMBIGUOUS",
    )
    candidate_service = _unique_service(
        values,
        _is_candidate_service,
        code="ORIGINAL_COMPANION_CANDIDATE_SERVICE_AMBIGUOUS",
    )
    backend = OriginalClientCompanionMutationServiceBackend(
        memory_service=memory_service,
        candidate_service=candidate_service,
    )
    try:
        mount_original_client_companion_mutation_api(
            app,
            backend,
            trusted_origins=trusted_origins,
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        raise OriginalClientMutationRuntimeError(
            "ORIGINAL_COMPANION_MUTATION_MOUNT_FAILED"
        ) from exc
    return backend


__all__ = [
    "OriginalClientMutationRuntimeError",
    "mount_original_client_companion_mutations_from_app",
]
