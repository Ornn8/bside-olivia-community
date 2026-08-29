"""Read-only B10B state to B08 composition bridge.

The bridge maps only B10B-validated, enabled provider settings into the
existing B08 factories.  It never copies provider assets, persists a credential,
starts an external process, or treats an unprobed LLM as ready.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .errors import B10BError


_ASR_ENV = {
    "provider": "ASR_PROVIDER",
    "server_url": "ASR_SERVER_URL",
    "language": "ASR_LANGUAGE",
    "runtime_root": "ASR_RUNTIME_ROOT",
    "runtime_executable": "ASR_RUNTIME_EXECUTABLE",
    "model_root": "ASR_MODEL_ROOT",
    "model_path": "ASR_MODEL_PATH",
    "cache_root": "ASR_CACHE_ROOT",
}
_TTS_ENV = {
    "profile": "OLIVIA_TTS_PROFILE",
    "provider": "OLIVIA_TTS_PROVIDER",
    "runtime_root": "OLIVIA_TTS_RUNTIME_ROOT",
    "model_dir": "OLIVIA_TTS_MODEL_DIR",
    "reference_audio": "OLIVIA_TTS_REFERENCE_AUDIO",
    "reference_text": "OLIVIA_TTS_REFERENCE_TEXT",
    "language": "OLIVIA_TTS_LANGUAGE",
    "fallback": "OLIVIA_TTS_FALLBACK",
    "fp16": "OLIVIA_TTS_FP16",
}
_MEMORY_ENV = {
    "OLIVIA_MEMORY_ENABLED",
    "OLIVIA_MEMORY_ROOT",
    "OLIVIA_MEMORY_PROVIDER",
    "OLIVIA_MEMORY_TTL_SECONDS",
    "OLIVIA_MEMORY_CONTEXT_MAX_CHARS",
}


def _as_environment(values: Mapping[str, Any], mapping: Mapping[str, str]) -> dict[str, str]:
    return {
        env_name: str(values[key])
        for key, env_name in mapping.items()
        if key in values and values[key] is not None
    }


def _verified_visual_settings(
    manager: Any,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    try:
        if manager.active_module_settings("visual-livetalking") is None:
            return None, None, False
        settings = manager.verified_active_module_settings("visual-livetalking")
    except B10BError as exc:
        if exc.code == "VERIFIED_PROFILE_UNVERIFIED":
            return None, "UNVERIFIED", True
        return None, "PIN_MISMATCH", True
    return settings, None, True


def _verified_provider_settings(manager: Any, module_id: str) -> dict[str, Any] | None:
    """Return provider settings only while the entire verified profile matches."""

    try:
        if manager.active_module_settings(module_id) is None:
            return None
        return manager.verified_active_module_settings(module_id)
    except B10BError:
        return None


def _is_b04_legacy_library(database: Path) -> bool:
    """Confirm B04's immutable legacy domain through a read-only SQLite handle."""

    try:
        uri = database.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name IN ('legacy_letters', 'legacy_letters_no_update', "
                "'legacy_letters_no_delete')"
            ).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        return False
    return set(rows) == {
        ("table", "legacy_letters"),
        ("trigger", "legacy_letters_no_update"),
        ("trigger", "legacy_letters_no_delete"),
    }


def _memory_database(settings: Mapping[str, Any]) -> Path:
    """Resolve a declared SQLite database without exposing it publicly."""

    raw = settings.get("database_path")
    if not isinstance(raw, str) or not raw.strip():
        raise B10BError(
            "MEMORY_DATABASE_INVALID",
            "The enabled B10B memory module has no usable SQLite reference.",
            {"module": "memory-local", "module_status": "CONFIG_INVALID"},
        )
    candidate = Path(raw).expanduser()
    database = (
        candidate
        if candidate.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}
        else candidate / "memory.sqlite3"
    )
    if database.is_symlink() or not database.is_file() or not _is_b04_legacy_library(database):
        raise B10BError(
            "MEMORY_DATABASE_INVALID",
            "The enabled B10B memory module does not reference a valid read-only legacy library.",
            {"module": "memory-local", "module_status": "UNAVAILABLE"},
        )
    return database


def _clear_memory_environment(values: dict[str, str]) -> None:
    """Remove memory environment bypasses before explicit port injection."""

    for key in _MEMORY_ENV:
        values.pop(key, None)


def bridge_health(manager: Any, *, visual_driver: Any | None = None) -> dict[str, Any]:
    """Report the bridge's offline readiness without accessing a provider."""

    verified_visual, verification_reason, b11_configured = _verified_visual_settings(manager)
    injected = visual_driver is not None and getattr(visual_driver, "_backend", None) is not None
    if injected:
        reason = "LLM_REACHABILITY_UNVERIFIED"
    elif verification_reason is not None:
        reason = verification_reason
    elif verified_visual is not None:
        reason = "B11_CAPTURE_DELEGATION_UNPROBED"
    elif b11_configured:
        reason = "UNVERIFIED"
    else:
        reason = "B11_TIMESTAMPED_VISUAL_DRIVER_UNAVAILABLE"
    return {
        "status": "DEGRADED",
        "ready": False,
        "reason": reason,
        "network_called": False,
        "visual_driver_injected": injected,
    }


def build_live_service_from_b10b(
    *,
    project_root: str | Path,
    data_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    visual_driver: Any | None = None,
    visual_request: Any | None = None,
) -> Any:
    """Build B08's existing ``LiveService`` from enabled B10B metadata.

    ``environ`` supplies ephemeral LLM configuration only.  B10B state supplies
    ASR/TTS references after its own validation; no secret-bearing setting can
    enter this function through B10B configuration.
    """

    from live import LiveService
    from .manager import B10BManager

    manager = B10BManager(project_root=project_root, data_root=data_root)
    if manager.active_module_settings("live-orchestration") is None:
        raise B10BError(
            "LIVE_ORCHESTRATION_NOT_ENABLED",
            "Enable the B10B live-orchestration module before starting B08.",
            {"module": "live-orchestration", "module_status": "NOT_ENABLED"},
        )

    values = dict(environ or {})
    project_path = Path(project_root).absolute()
    if (
        "OLIVIA_PERSONA_V2_FILE" not in values
        and not (project_path / "llm_config.json").is_file()
        and not (project_path / "linli_character" / "persona_release_v2.json").is_file()
    ):
        packaged_persona = (
            Path(__file__).resolve().parents[3]
            / "linli_character"
            / "persona_release_v2.json"
        )
        if packaged_persona.is_file():
            values["OLIVIA_PERSONA_V2_FILE"] = str(packaged_persona)
    # ASR/TTS process references may enter only through the verified B10B
    # profile.  LLM configuration remains intentionally ephemeral.
    for env_name in tuple(values):
        if env_name.startswith("ASR_") or env_name.startswith("OLIVIA_TTS_"):
            values.pop(env_name, None)

    _clear_memory_environment(values)
    memory_settings = manager.active_module_settings("memory-local")
    if memory_settings is None:
        from memory_port import NullMemoryPort

        memory_port = NullMemoryPort()
    else:
        from local_memory import LocalMemoryAdapter, UnavailableMemoryPort

        try:
            memory_port = LocalMemoryAdapter(
                _memory_database(memory_settings),
                conversation_enabled=False,
                read_only=True,
            )
        except (OSError, sqlite3.Error, ValueError):
            memory_port = UnavailableMemoryPort("sqlite adapter unavailable", provider="sqlite")

    asr = _verified_provider_settings(manager, "asr-local")
    if asr is None:
        values["ASR_PROVIDER"] = "text-fallback"
    else:
        values.update(_as_environment(asr, _ASR_ENV))

    tts = _verified_provider_settings(manager, "tts-local")
    if tts is None:
        values["OLIVIA_TTS_ENABLED"] = "false"
    else:
        values.update(_as_environment(tts, _TTS_ENV))
        # B06's verified runtime keeps its own dependency venv.  Delegate to
        # that already-installed interpreter instead of requiring the B08
        # process to duplicate CosyVoice's heavy Python dependencies.
        runtime_root = Path(str(tts.get("runtime_root", "")))
        external_python = runtime_root / "venv" / "Scripts" / "python.exe"
        if external_python.is_file():
            values["OLIVIA_TTS_EXTERNAL_PYTHON"] = str(external_python)
        values["OLIVIA_TTS_ENABLED"] = "true"

    if visual_driver is None:
        visual, _verification_reason, _configured = _verified_visual_settings(manager)
        if visual is not None:
            # The B11 configuration remains reference-only.  This only gives
            # B07 a delegating backend; the external worker still owns every
            # frame inference and temporary media is kept under B10B evidence.
            from runtime.visual.livetalking import LiveTalkingConfig
            from runtime.visual.livetalking_backend import LiveTalkingVisualBackend
            from visual_driver import VisualDriver

            settings = {key: value for key, value in visual.items() if key != "managed_external_copies"}
            try:
                visual_driver = VisualDriver(
                    LiveTalkingVisualBackend(
                        LiveTalkingConfig(**settings),
                        evidence_root=manager.data_root / ".evidence",
                    )
                )
            except (TypeError, ValueError):
                # Invalid external B11 settings must leave B08 in its original
                # visual fallback mode; composition does not probe or repair.
                visual_driver = VisualDriver()
        else:
            from visual_driver import VisualDriver

            visual_driver = VisualDriver()

    service = LiveService.from_environment(
        environ=values,
        project_root=str(project_path),
        memory_port=memory_port,
        visual_driver=visual_driver,
        visual_request=visual_request,
    )
    return service


__all__ = ["bridge_health", "build_live_service_from_b10b"]
