"""B08 composition root for the already-validated local provider boundaries.

This module only assembles B03/B04/B05/B06/B07 adapters.  It never starts a
provider, downloads an asset, or invents readiness; each existing provider
continues to own its own lifecycle and health contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from asr.config import AsrConfig
from asr.provider import create_provider
from llm_gateway import (
    Gateway,
    GatewayConfig,
    UnconfiguredAdapter,
    api_key_configured,
    create_gateway,
    load_gateway_config,
)
from local_memory import MemoryConfig, create_memory_adapter, load_memory_config
from memory_port import MemoryPort, NullMemoryPort
from persona_loader import PersonaSnapshot as PersonaV2Snapshot, load_persona
from persona_provider import (
    CompositePersonaEvidencePort,
    ConfigPersonaProvider,
    JsonPersonaEvidencePort,
    MemoryReferenceEvidencePort,
)
from tts import TTSConfig, TTSProfileManager, TTSService
from visual_driver import VisualDriver


_TTS_ENV_FIELDS = {
    "OLIVIA_TTS_PROFILE": "profile",
    "OLIVIA_TTS_PROVIDER": "provider",
    "OLIVIA_TTS_ENABLED": "enabled",
    "OLIVIA_TTS_RUNTIME_ROOT": "runtime_root",
    "OLIVIA_TTS_MODEL_DIR": "model_dir",
    "OLIVIA_TTS_REFERENCE_AUDIO": "reference_audio",
    "OLIVIA_TTS_REFERENCE_TEXT": "reference_text",
    "OLIVIA_TTS_LANGUAGE": "language",
    "OLIVIA_TTS_LICENSE_ID": "license_id",
    "OLIVIA_TTS_FALLBACK": "fallback",
    "OLIVIA_TTS_SPEED": "speed",
    "OLIVIA_TTS_LEADING_TRIM_SECONDS": "leading_trim_seconds",
    "OLIVIA_TTS_MAX_INPUT_CHARS": "max_input_chars",
    "OLIVIA_TTS_FP16": "fp16",
}

# The CosyVoice installation may intentionally live in a separate Python venv.
# Pass the separate interpreter path as an adapter option; the B08 process does
# not import that runtime.
_TTS_PROVIDER_OPTION_ENV = {
    "OLIVIA_TTS_EXTERNAL_PYTHON": "external_python",
}


@dataclass(frozen=True)
class UnavailableAsrProvider:
    """Fail-closed status object used only when B05 config cannot be built."""

    reason: str
    provider: str = "none"

    def status(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "status": "unavailable",
            "ready": False,
            "is_asr": True,
            "reason": self.reason,
            "network_called": False,
        }


@dataclass(frozen=True)
class UnavailableTtsService:
    """Fail-closed B06 boundary used when the service cannot be constructed."""

    reason_code: str = "TTS_CONFIG_INVALID"

    def health(self) -> dict[str, object]:
        return {
            "status": "unavailable",
            "ready": False,
            "provider": "none",
            "reason_code": self.reason_code,
            "fallback": "text",
        }

    async def start(self, _request: Any) -> Any:
        raise RuntimeError(self.reason_code)

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class _PersonaV2Provider:
    value: PersonaV2Snapshot

    def persona_v2_snapshot(self) -> PersonaV2Snapshot:
        return self.value


@dataclass
class LiveEnvironment:
    """Concrete B08 dependency bundle plus path-free construction evidence."""

    gateway_config: GatewayConfig
    memory_config: MemoryConfig
    asr_config: AsrConfig | None
    tts_config: TTSConfig
    gateway: Gateway
    memory_port: MemoryPort
    persona_provider: Any
    asr_provider: Any
    tts_service: TTSService
    visual_driver: VisualDriver
    visual_request: Any | None = None
    construction_errors: Mapping[str, str] = field(default_factory=dict)
    network_called: bool = False
    llm_api_key_configured: bool = False

    def public_dict(self) -> dict[str, Any]:
        """Return a deterministic report without paths, secrets, or payloads."""

        return {
            "schema_version": 1,
            "environment_kind": "b08_live_composition",
            "network_called": self.network_called,
            "components": {
                "llm": self._llm_public(),
                "memory": self._status_public(self.memory_port, fallback={"status": "unavailable"}),
                "asr": self._status_public(self.asr_provider, fallback={"status": "unavailable", "ready": False}),
                "tts": self._status_public(self.tts_service, fallback={"status": "unavailable"}),
                "visual": {
                    "status": "available"
                    if getattr(self.visual_driver, "_backend", None) is not None
                    else "unavailable",
                    "ready": getattr(self.visual_driver, "_backend", None) is not None,
                    "fallback": "original_static_or_clip",
                    "media_written": False,
                },
            },
            "construction_errors": dict(self.construction_errors),
            "payload_policy": {
                "trace_text": False,
                "trace_owner_id": False,
                "trace_audio": False,
                "trace_frame_payload": False,
                "replacement_media_generated": False,
            },
        }

    def _llm_public(self) -> dict[str, Any]:
        configured = self.gateway_config.provider not in {"", "none"}
        if not configured or isinstance(self.gateway, UnconfiguredAdapter):
            return {
                "provider": self.gateway_config.provider if configured else "none",
                "status": "unavailable",
                "ready": False,
                "configured": configured,
                "stream": bool(getattr(self.gateway, "stream_enabled", False)),
                "api_key_configured": self.llm_api_key_configured if configured else False,
            }
        if self.gateway_config.provider == "mock":
            status = "available"
            ready = True
        elif not self.gateway_config.feature_enabled or not self.gateway_config.base_url or not self.gateway_config.model:
            status = "unavailable"
            ready = False
        elif self.gateway_config.requires_api_key and not self.llm_api_key_configured:
            status = "unavailable"
            ready = False
        else:
            status = "degraded"
            ready = False
        return {
            "provider": self.gateway_config.provider,
            "status": status,
            "ready": ready,
            "configured": configured,
            "stream": bool(getattr(self.gateway, "stream_enabled", False)),
            "api_key_configured": self.llm_api_key_configured,
        }

    @staticmethod
    def _status_public(provider: Any, *, fallback: Mapping[str, Any]) -> dict[str, Any]:
        try:
            raw = provider.status() if callable(getattr(provider, "status", None)) else provider.health()
            raw = dict(raw)
        except Exception:
            raw = dict(fallback)
        allowed = {
            "provider",
            "status",
            "ready",
            "is_asr",
            "reason",
            "reason_code",
            "verified",
            "fallback",
            "license_id",
            "model",
            "media_written",
            "network_called",
        }
        public = {key: value for key, value in raw.items() if key in allowed}
        status = str(public.get("status", "unavailable")).casefold()
        public["ready"] = bool(
            status in {"available", "ready"} and raw.get("ready", True) is not False
        )
        return public


def _resolve(path: str | os.PathLike[str] | None, root: Path) -> Path | None:
    if path is None or str(path).strip() == "":
        return None
    value = Path(path)
    return value if value.is_absolute() else root / value


def _load_asr_config(
    environ: Mapping[str, str],
    *,
    project_root: Path,
    path: str | os.PathLike[str] | None,
) -> AsrConfig:
    configured_path = _resolve(path or environ.get("ASR_CONFIG_PATH"), project_root)
    if configured_path is not None:
        return AsrConfig.from_json(configured_path)
    return AsrConfig.from_env(dict(environ))


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _load_tts_config(environ: Mapping[str, str], *, project_root: Path) -> TTSConfig:
    state_root = _resolve(environ.get("OLIVIA_TTS_STATE_ROOT"), project_root)
    profile = environ.get("OLIVIA_TTS_PROFILE", "cosyvoice3-live")
    if state_root is not None and (state_root / f"{profile}.json").is_file():
        return TTSProfileManager(state_root).config(profile)

    values: dict[str, Any] = {}
    for env_name, field_name in _TTS_ENV_FIELDS.items():
        if env_name in environ:
            values[field_name] = environ[env_name]
    provider_options = {
        option: environ[env_name]
        for env_name, option in _TTS_PROVIDER_OPTION_ENV.items()
        if environ.get(env_name, "").strip()
    }
    if provider_options:
        values["provider_options"] = provider_options
    # The B06 provider is opt-in.  An absent profile must not cause a model
    # constructor or a network/download path to run during B08 startup.
    values.setdefault("enabled", _as_bool(environ.get("OLIVIA_TTS_ENABLED"), False))
    values.setdefault("profile", profile)
    values.setdefault("provider", environ.get("OLIVIA_TTS_PROVIDER", "cosyvoice3"))
    return TTSConfig.from_mapping(values)


def build_live_environment(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: str | os.PathLike[str] | None = None,
    llm_config_path: str | os.PathLike[str] | None = None,
    memory_config_path: str | os.PathLike[str] | None = None,
    memory_port: MemoryPort | None = None,
    asr_config_path: str | os.PathLike[str] | None = None,
    visual_driver: VisualDriver | None = None,
    visual_request: Any | None = None,
) -> LiveEnvironment:
    """Compose local providers from existing factories without probing them."""

    env = dict(os.environ if environ is None else environ)
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[1]
    root = root.absolute()
    errors: dict[str, str] = {}

    try:
        llm_path = _resolve(llm_config_path, root) or root / "llm_config.json"
        gateway_config = load_gateway_config(llm_path, environ=env)
        gateway = create_gateway(gateway_config)
    except Exception:
        gateway_config = GatewayConfig(provider="none")
        gateway = UnconfiguredAdapter()
        errors["llm"] = "LLM_UNAVAILABLE"

    if memory_port is not None:
        try:
            injected_status = memory_port.status()
            memory_config = MemoryConfig(
                enabled=injected_status.get("conversation_enabled") is True,
                provider=str(injected_status.get("provider", "none")),
            )
        except Exception:
            memory_config = MemoryConfig(enabled=False, config_error="MEMORY_UNAVAILABLE")
            errors["memory"] = "MEMORY_UNAVAILABLE"
    else:
        try:
            memory_path = _resolve(memory_config_path, root)
            memory_config = load_memory_config(memory_path, environ=env, root=root)
            memory_port = create_memory_adapter(memory_config, environ=env)
        except Exception:
            memory_config = MemoryConfig(enabled=False, config_error="MEMORY_UNAVAILABLE")
            memory_port = NullMemoryPort()
            errors["memory"] = "MEMORY_UNAVAILABLE"

    persona_file = _resolve(gateway_config.persona_file, root) or root / "linli_character/system_prompt.md"
    persona_config = _resolve(gateway_config.persona_config, root) or root / "linli_character/persona_config.json"
    persona_evidence = _resolve(gateway_config.persona_evidence_file, root) or root / "linli_character/provenance.json"
    try:
        legacy_persona_provider = ConfigPersonaProvider(
            persona_config,
            draft_path=persona_file,
            evidence_port=CompositePersonaEvidencePort(
                JsonPersonaEvidencePort(persona_evidence),
                MemoryReferenceEvidencePort(memory_port),
            ),
            feature_enabled=gateway_config.feature_enabled,
        )
        persona_provider = legacy_persona_provider
        if gateway_config.persona_v2_enabled:
            persona_v2_file = (
                _resolve(gateway_config.persona_v2_file, root)
                or root / "linli_character/persona_release_v2.json"
            )
            persona_provider = _PersonaV2Provider(
                load_persona(persona_v2_file).snapshot
            )
    except Exception:
        persona_provider = None
        errors["persona"] = "PERSONA_UNAVAILABLE"

    asr_config: AsrConfig | None = None
    try:
        asr_config = _load_asr_config(env, project_root=root, path=asr_config_path)
        asr_provider = create_provider(asr_config)
    except Exception:
        provider_name = str(env.get("ASR_PROVIDER", "none"))
        asr_provider = UnavailableAsrProvider("ASR_CONFIG_INVALID", provider=provider_name)
        errors["asr"] = "ASR_CONFIG_INVALID"

    try:
        tts_config = _load_tts_config(env, project_root=root)
        tts_service = TTSService(tts_config)
    except Exception:
        tts_config = TTSConfig(enabled=False, fallback="unavailable")
        try:
            tts_service = TTSService(tts_config)
        except Exception:
            tts_service = UnavailableTtsService()
        errors["tts"] = "TTS_CONFIG_INVALID"

    driver = visual_driver if visual_driver is not None else VisualDriver()
    return LiveEnvironment(
        gateway_config=gateway_config,
        memory_config=memory_config,
        asr_config=asr_config,
        tts_config=tts_config,
        gateway=gateway,
        memory_port=memory_port,
        persona_provider=persona_provider,
        asr_provider=asr_provider,
        tts_service=tts_service,
        visual_driver=driver,
        visual_request=visual_request,
        construction_errors=errors,
        llm_api_key_configured=api_key_configured(gateway_config, environ=env),
    )


__all__ = [
    "LiveEnvironment",
    "UnavailableAsrProvider",
    "UnavailableTtsService",
    "build_live_environment",
]
