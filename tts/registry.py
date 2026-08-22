"""Provider registry with lazy imports and explicit custom-provider support."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .contracts import AudioChunk, TTSConfig, TTSRequest, TTSUnavailable


class TTSProvider(Protocol):
    name: str
    license_id: str

    def health(self) -> dict[str, Any]: ...

    def stream_sentence(
        self, text: str, request: TTSRequest, sentence_index: int
    ): ...

    def close(self) -> None: ...


ProviderFactory = Callable[[TTSConfig], TTSProvider]


@dataclass(frozen=True)
class ProviderRegistration:
    name: str
    factory: ProviderFactory
    license_id: str


class TTSProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderRegistration] = {}

    def register(self, name: str, factory: ProviderFactory, *, license_id: str) -> None:
        normalized = str(name).strip().lower()
        if not normalized:
            raise ValueError("provider name is required")
        self._providers[normalized] = ProviderRegistration(normalized, factory, license_id)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def registration(self, name: str) -> ProviderRegistration:
        try:
            return self._providers[str(name).strip().lower()]
        except KeyError as exc:
            raise TTSUnavailable("TTS_PROVIDER_UNKNOWN", "provider is not registered") from exc

    def create(self, config: TTSConfig) -> TTSProvider:
        registration = self.registration(config.provider)
        provider = registration.factory(config)
        if getattr(provider, "license_id", registration.license_id) != registration.license_id:
            raise TTSUnavailable("TTS_LICENSE_MISMATCH", "provider license metadata mismatch")
        return provider


def default_registry() -> TTSProviderRegistry:
    from .providers import CosyVoice3Provider

    registry = TTSProviderRegistry()
    registry.register("cosyvoice3", CosyVoice3Provider, license_id="Apache-2.0")
    return registry
