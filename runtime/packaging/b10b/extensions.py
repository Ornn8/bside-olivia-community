"""Stable provider extension boundary for future B06 and later tranches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderContext:
    """Non-secret context passed to an optional provider health adapter."""

    project_root: Path
    data_root: Path
    module_id: str
    config: dict[str, Any]


class ModuleProvider(Protocol):
    """Provider contract implemented by a component tranche after merge."""

    api_version: str

    def health(self, context: ProviderContext) -> dict[str, Any]:
        """Return a truthful status without copying or mutating external assets."""


_PROVIDERS: dict[str, ModuleProvider] = {}


def register_provider(provider_id: str, provider: ModuleProvider) -> None:
    normalized = str(provider_id).strip().lower()
    if not normalized:
        raise ValueError("provider_id is required")
    if getattr(provider, "api_version", None) != "b10b.provider.v1":
        raise ValueError("provider must implement b10b.provider.v1")
    _PROVIDERS[normalized] = provider


def unregister_provider(provider_id: str) -> None:
    _PROVIDERS.pop(str(provider_id).strip().lower(), None)


def registered_provider_ids() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def provider_health(provider_id: str, context: ProviderContext) -> dict[str, Any]:
    provider = _PROVIDERS.get(str(provider_id).strip().lower())
    if provider is None:
        return {
            "status": "UNAVAILABLE",
            "reason": "PROVIDER_NOT_REGISTERED",
            "provider_id": str(provider_id).strip().lower(),
            "api_version": "b10b.provider.v1",
        }
    try:
        value = provider.health(context)
    except Exception:
        return {
            "status": "UNAVAILABLE",
            "reason": "PROVIDER_HEALTH_ERROR",
            "provider_id": str(provider_id).strip().lower(),
            "api_version": "b10b.provider.v1",
        }
    if not isinstance(value, dict) or value.get("status") not in {
        "HEALTHY",
        "DEGRADED",
        "UNAVAILABLE",
    }:
        return {
            "status": "UNAVAILABLE",
            "reason": "PROVIDER_HEALTH_INVALID",
            "provider_id": str(provider_id).strip().lower(),
            "api_version": "b10b.provider.v1",
        }
    return {"provider_id": str(provider_id).strip().lower(), "api_version": "b10b.provider.v1", **value}
