"""Live conversation orchestration and provider-neutral lifecycle contracts."""

from __future__ import annotations

from typing import Any

from asr.fallback import TextFallbackProvider
from llm_gateway import Gateway, UnconfiguredAdapter, api_key_configured
from memory_port import MemoryPort, NullMemoryPort
from tts import TTSService
from visual_driver import VisualDriver

from .contracts import (
    LiveConfig,
    LiveError,
    LiveEvent,
    LiveSessionState,
    LiveTurnHandle,
    LiveTurnResult,
    replay_trace,
)
from .session import LiveSession
from .environment import LiveEnvironment, build_live_environment


class LiveService:
    """Small public facade for the B08 component health boundary.

    The full session engine is added behind this stable facade in subsequent
    vertical slices.  Provider objects are injected so local installations can
    replace or disable one component without changing the session API.
    """

    def __init__(
        self,
        *,
        gateway: Gateway | None = None,
        memory_port: MemoryPort | None = None,
        persona_provider: Any | None = None,
        asr_provider: Any | None = None,
        tts_service: TTSService | None = None,
        visual_driver: VisualDriver | None = None,
        visual_request: Any | None = None,
        config: LiveConfig | None = None,
    ) -> None:
        self.gateway = gateway if gateway is not None else UnconfiguredAdapter()
        self.memory_port = memory_port if memory_port is not None else NullMemoryPort()
        self.persona_provider = persona_provider
        self.asr_provider = asr_provider if asr_provider is not None else TextFallbackProvider()
        self.tts_service = tts_service
        self.visual_driver = visual_driver
        self.visual_request = visual_request
        self.config = config or LiveConfig()
        self._sessions: dict[str, LiveSession] = {}
        self.environment: LiveEnvironment | None = None
        self._resources_closed = False

    @classmethod
    def from_environment(
        cls,
        *,
        environ: dict[str, str] | None = None,
        project_root: str | None = None,
        llm_config_path: str | None = None,
        memory_config_path: str | None = None,
        memory_port: MemoryPort | None = None,
        asr_config_path: str | None = None,
        visual_driver: VisualDriver | None = None,
        visual_request: Any | None = None,
        config: LiveConfig | None = None,
    ) -> "LiveService":
        environment = build_live_environment(
            environ=environ,
            project_root=project_root,
            llm_config_path=llm_config_path,
            memory_config_path=memory_config_path,
            memory_port=memory_port,
            asr_config_path=asr_config_path,
            visual_driver=visual_driver,
            visual_request=visual_request,
        )
        service = cls(
            gateway=environment.gateway,
            memory_port=environment.memory_port,
            persona_provider=environment.persona_provider,
            asr_provider=environment.asr_provider,
            tts_service=environment.tts_service,
            visual_driver=environment.visual_driver,
            visual_request=environment.visual_request,
            config=config,
        )
        service.environment = environment
        return service

    async def start_session(self, owner_id: str, *, session_id: str | None = None) -> LiveSession:
        if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 128:
            raise LiveError("LIVE_INVALID_INPUT")
        session = LiveSession(
            session_id=session_id or __import__("uuid").uuid4().hex,
            owner_id=owner_id,
            gateway=self.gateway,
            memory_port=self.memory_port,
            persona_provider=self.persona_provider,
            asr_provider=self.asr_provider,
            tts_service=self.tts_service,
            visual_driver=self.visual_driver,
            visual_request=self.visual_request,
            config=self.config,
        )
        if session.session_id in self._sessions:
            raise LiveError("LIVE_INVALID_INPUT")
        self._sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str, owner_id: str) -> LiveSession:
        session = self._sessions.get(str(session_id))
        if session is None:
            raise LiveError("LIVE_SESSION_NOT_FOUND")
        if session.owner_id != owner_id:
            raise LiveError("LIVE_SESSION_FORBIDDEN")
        return session

    async def stop(self) -> None:
        for session in tuple(self._sessions.values()):
            await session.close()
        self._sessions.clear()
        if not self._resources_closed:
            for resource in (self.tts_service, self.memory_port, self.visual_driver):
                close = getattr(resource, "close", None)
                if callable(close):
                    close()
            self._resources_closed = True

    def health(self) -> dict[str, Any]:
        components = {
            "llm": self._llm_health(),
            "memory": self._memory_health(),
            "asr": self._asr_health(),
            "tts": self._tts_health(),
            "visual": self._visual_health(),
        }
        if components["llm"]["status"] == "UNAVAILABLE":
            status = "UNAVAILABLE"
        elif any(component["status"] != "READY" for component in components.values()):
            status = "DEGRADED"
        else:
            status = "READY"
        result = {
            "status": status,
            "ready": status == "READY",
            "components": components,
            "network_called": False,
        }
        return result

    def _llm_health(self) -> dict[str, Any]:
        if isinstance(self.gateway, UnconfiguredAdapter):
            return {
                "status": "UNAVAILABLE",
                "ready": False,
                "provider": "none",
                "reason_code": "LLM_UNAVAILABLE",
                "fallback": "safe_static_or_error",
            }
        config = getattr(self.gateway, "config", None)
        if config is None:
            primary = getattr(self.gateway, "primary", None)
            config = getattr(primary, "config", None)
        if config is not None:
            key_configured = (
                self.environment.llm_api_key_configured
                if self.environment is not None
                else api_key_configured(config)
            )
            if config.provider == "mock":
                return {
                    "status": "READY",
                    "ready": True,
                    "provider": "mock",
                    "fallback": "none",
                    "reason_code": None,
                    "network_called": False,
                }
            if not config.feature_enabled or not config.base_url or not config.model:
                return {
                    "status": "UNAVAILABLE",
                    "ready": False,
                    "provider": config.provider or "none",
                    "reason_code": "LLM_UNAVAILABLE",
                    "fallback": "safe_static_or_error",
                    "network_called": False,
                }
            if config.requires_api_key and not key_configured:
                return {
                    "status": "UNAVAILABLE",
                    "ready": False,
                    "provider": config.provider,
                    "reason_code": "LLM_API_KEY_UNAVAILABLE",
                    "fallback": "safe_static_or_error",
                    "network_called": False,
                }
            return {
                "status": "DEGRADED",
                "ready": False,
                "provider": config.provider,
                "reason_code": "LLM_REACHABILITY_UNVERIFIED",
                "fallback": "safe_static_or_error",
                "network_called": False,
            }
        return {
            "status": "DEGRADED",
            "ready": False,
            "provider": type(self.gateway).__name__,
            "reason_code": "LLM_REACHABILITY_UNVERIFIED",
            "fallback": "safe_static_or_error",
            "network_called": False,
        }

    def _memory_health(self) -> dict[str, Any]:
        try:
            raw = dict(self.memory_port.status())
        except Exception:
            raw = {"status": "unavailable", "provider": "none"}
        available = raw.get("status") == "available"
        return {
            "status": "READY" if available else "DEGRADED",
            "ready": available,
            "provider": raw.get("provider", "none") if available else "none",
            "fallback": "session_only",
            "reason_code": None if available else "MEMORY_UNAVAILABLE",
        }

    def _asr_health(self) -> dict[str, Any]:
        try:
            raw = dict(self.asr_provider.status())
        except Exception:
            raw = {"status": "unavailable", "provider": "none", "ready": False}
        is_native_ready = raw.get("status") == "available" and raw.get("ready") is True and raw.get("is_asr", True)
        return {
            "status": "READY" if is_native_ready else "DEGRADED",
            "ready": is_native_ready,
            "provider": raw.get("provider", "none"),
            "fallback": "text_input",
            "reason_code": None if is_native_ready else str(raw.get("reason", "ASR_UNAVAILABLE")),
        }

    def _tts_health(self) -> dict[str, Any]:
        if self.tts_service is None:
            raw: dict[str, Any] = {"status": "unavailable", "provider": "none"}
        else:
            try:
                raw = dict(self.tts_service.health())
            except Exception:
                raw = {"status": "unavailable", "provider": "none"}
        available = raw.get("status") == "available"
        return {
            "status": "READY" if available else "UNAVAILABLE",
            "ready": available,
            "provider": raw.get("provider", "none") if available else "none",
            "fallback": "text_output",
            "reason_code": None if available else str(raw.get("reason_code", "TTS_UNAVAILABLE")),
        }

    def _visual_health(self) -> dict[str, Any]:
        # B07's driver is deliberately conservative: without an injected
        # backend, it can only return its exact original-frame fallback.
        driven = self.visual_driver is not None and getattr(self.visual_driver, "_backend", None) is not None
        return {
            "status": "READY" if driven else "DEGRADED",
            "ready": driven,
            "provider": "original-visual-driver" if driven else "none",
            "fallback": "original_static_or_clip",
            "reason_code": None if driven else "VISUAL_UNAVAILABLE",
        }


__all__ = [
    "LiveConfig",
    "LiveError",
    "LiveEvent",
    "LiveService",
    "LiveSession",
    "LiveSessionState",
    "LiveTurnHandle",
    "LiveTurnResult",
    "LiveEnvironment",
    "build_live_environment",
    "replay_trace",
]
