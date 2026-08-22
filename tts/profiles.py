"""Reversible local profile state; model/reference files stay external."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .contracts import TTSConfig, TTSValidationError
from .registry import TTSProviderRegistry, default_registry


_PROFILE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class TTSProfileManager:
    """Manage only profile metadata under an explicitly supplied state root."""

    schema_version = "b06.tts-profile.v1"

    def __init__(self, state_root: str | Path, *, registry: TTSProviderRegistry | None = None) -> None:
        self.state_root = Path(state_root)
        self.profiles_root = self.state_root / "profiles"
        self.registry = registry or default_registry()

    def _profile_path(self, name: str) -> Path:
        normalized = str(name).strip()
        if not _PROFILE_NAME.fullmatch(normalized):
            raise TTSValidationError("TTS_INVALID_PROFILE", "profile name is invalid")
        return self.profiles_root / f"{normalized}.json"

    def _read(self, name: str) -> dict[str, Any]:
        path = self._profile_path(name)
        if not path.is_file():
            raise TTSValidationError("TTS_PROFILE_NOT_FOUND", "profile is not installed")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TTSValidationError("TTS_PROFILE_INVALID", "profile metadata is unreadable") from exc
        if value.get("schema_version") != self.schema_version:
            raise TTSValidationError("TTS_PROFILE_INVALID", "unsupported profile schema")
        return value

    @staticmethod
    def _external_health(config: TTSConfig) -> dict[str, Any]:
        def state(value: str, *, directory: bool = False) -> bool:
            if not value:
                return False
            path = Path(value)
            return path.is_dir() if directory else path.is_file()

        return {
            "runtime_root_exists": state(config.runtime_root, directory=True),
            "model_dir_exists": state(config.model_dir, directory=True),
            "reference_audio_exists": state(config.reference_audio),
        }

    def install(self, config: TTSConfig, *, dry_run: bool = False) -> dict[str, Any]:
        path = self._profile_path(config.profile)
        if config.provider == "cosyvoice3":
            health = self._external_health(config)
            if not all(health.values()):
                missing = [key for key, present in health.items() if not present]
                raise TTSValidationError("TTS_EXTERNAL_ASSET_MISSING", ",".join(missing))
        existing = path.is_file()
        payload = {
            "schema_version": self.schema_version,
            "profile": config.profile,
            "status": "enabled" if config.enabled else "disabled",
            "config": {
                "profile": config.profile,
                "provider": config.provider,
                "enabled": config.enabled,
                "runtime_root": config.runtime_root,
                "model_dir": config.model_dir,
                "reference_audio": config.reference_audio,
                "reference_text": config.reference_text,
                "language": config.language,
                "license_id": config.license_id,
                "fallback": config.fallback,
                "speed": config.speed,
                "leading_trim_seconds": config.leading_trim_seconds,
                "max_input_chars": config.max_input_chars,
                "fp16": config.fp16,
                "provider_options": dict(config.provider_options),
            },
            "external_assets": "referenced_only; no model or audio copied",
        }
        result = {
            "status": "NO_OP" if existing else "INSTALLED",
            "profile": config.profile,
            "dry_run": dry_run,
            "owned_path": str(path.relative_to(self.state_root)).replace("\\", "/"),
            "external_assets_copied": False,
        }
        if not dry_run:
            _atomic_json(path, payload)
        return result

    def config(self, name: str) -> TTSConfig:
        value = self._read(name)
        return TTSConfig.from_mapping(value["config"])

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        value = self._read(name)
        config = TTSConfig.from_mapping(value["config"])
        config = replace(config, enabled=bool(enabled))
        value["config"] = {
            **value["config"],
            "enabled": config.enabled,
        }
        value["status"] = "enabled" if config.enabled else "disabled"
        _atomic_json(self._profile_path(name), value)
        return {"status": "ENABLED" if enabled else "DISABLED", "profile": config.profile}

    def customize(self, name: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        value = self._read(name)
        raw = dict(value["config"])
        allowed = {
            "provider",
            "runtime_root",
            "model_dir",
            "reference_audio",
            "reference_text",
            "language",
            "license_id",
            "fallback",
            "speed",
            "leading_trim_seconds",
            "max_input_chars",
            "fp16",
            "provider_options",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise TTSValidationError("TTS_CUSTOMIZE_FIELD_UNKNOWN", ",".join(unknown))
        raw.update(dict(changes))
        raw["profile"] = name
        config = TTSConfig.from_mapping(raw)
        value["config"] = {
            **raw,
            "enabled": config.enabled,
            "provider_options": dict(config.provider_options),
        }
        _atomic_json(self._profile_path(name), value)
        return {"status": "CUSTOMIZED", "profile": name, "config": config.public_dict()}

    def uninstall(self, name: str, *, dry_run: bool = True) -> dict[str, Any]:
        path = self._profile_path(name)
        if not path.is_file():
            return {
                "status": "NOT_INSTALLED",
                "profile": name,
                "dry_run": dry_run,
                "external_assets_deleted": False,
            }
        result = {
            "status": "DRY_RUN" if dry_run else "UNINSTALLED",
            "profile": name,
            "dry_run": dry_run,
            "owned_path": str(path.relative_to(self.state_root)).replace("\\", "/"),
            "external_assets_deleted": False,
        }
        if not dry_run:
            path.unlink()
        return result

    def doctor(self, name: str) -> dict[str, Any]:
        config = self.config(name)
        try:
            provider = self.registry.create(config)
            health = provider.health()
            provider.close()
        except Exception as exc:
            health = {"status": "unavailable", "reason_code": getattr(exc, "code", "TTS_DOCTOR_ERROR")}
        return {
            "status": "HEALTHY" if health.get("status") == "available" else "UNAVAILABLE",
            "profile": name,
            "config": config.public_dict(),
            "provider": health,
            "external_assets": self._external_health(config),
        }

    def list_profiles(self) -> list[str]:
        if not self.profiles_root.is_dir():
            return []
        return sorted(path.stem for path in self.profiles_root.glob("*.json"))
