"""Layered B10A configuration with environment-only provider secrets."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .errors import B10AError
from .security import redact


CONFIG_SCHEMA_VERSION = "b10a.config.v1"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "profile": "skeleton",
    "providers": {
        "llm-api": {
            "type": "openai-compatible",
            "base_url_env": "B10A_LLM_API_BASE_URL",
            "api_key_env": "B10A_LLM_API_KEY",
            "model_env": "B10A_LLM_MODEL",
        },
        "memory-local": {"type": "local", "root": "memory"},
        "asr-local": {"type": "local", "root": "asr"},
        "tts-local": {"type": "local", "root": "tts"},
        "visual-driver": {"type": "local", "root": "visual"},
        "media-original": {"type": "path-reference-only", "root": "original-reference"},
    },
    "logging": {"level": "info", "redact_secrets": True},
}

_TOP_LEVEL = {"schema_version", "profile", "providers", "logging"}
_PROVIDER_FIELDS = {
    "type",
    "base_url",
    "base_url_env",
    "api_key_env",
    "model",
    "model_env",
    "root",
    "enabled",
    "options",
    "api_key",
    "token",
    "secret",
}
_LOGGING_FIELDS = {"level", "redact_secrets"}
_SECRET_FIELDS = {"api_key", "token", "secret", "password", "authorization"}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_layer(path: Path, *, source: str, allow_secret_fields: bool) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise B10AError(
            "CONFIG_INVALID",
            "A B10A configuration layer is unreadable or invalid JSON.",
            {"source": source},
        ) from exc
    if not isinstance(value, dict):
        raise B10AError("CONFIG_INVALID", "A B10A configuration layer must be an object.", {"source": source})
    unknown = sorted(set(value) - _TOP_LEVEL)
    if unknown:
        raise B10AError(
            "CONFIG_INVALID",
            "A B10A configuration layer contains unsupported top-level keys.",
            {"source": source, "keys": unknown},
        )
    if "schema_version" in value and value["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise B10AError("CONFIG_INVALID", "A B10A configuration layer has an unsupported schema version.")
    providers = value.get("providers", {})
    if not isinstance(providers, dict):
        raise B10AError("CONFIG_INVALID", "The providers configuration must be an object.")
    for provider_id, provider in providers.items():
        if not isinstance(provider_id, str) or not isinstance(provider, dict):
            raise B10AError("CONFIG_INVALID", "Each provider configuration must be an object.")
        unknown_provider = sorted(set(provider) - _PROVIDER_FIELDS)
        if unknown_provider:
            raise B10AError(
                "CONFIG_INVALID",
                "A provider configuration contains unsupported keys.",
                {"provider": provider_id, "keys": unknown_provider},
            )
        for secret_field in _SECRET_FIELDS.intersection(provider):
            if not isinstance(provider[secret_field], str):
                raise B10AError(
                    "CONFIG_INVALID",
                    "Provider secret fields must be strings when supplied in ignored local config.",
                    {"source": source, "provider": provider_id},
                )
        if not allow_secret_fields and _SECRET_FIELDS.intersection(provider):
            raise B10AError(
                "CONFIG_INVALID",
                "Provider secrets may only appear in the ignored local config layer or environment.",
                {"source": source, "provider": provider_id},
            )
    logging = value.get("logging", {})
    if not isinstance(logging, dict) or not set(logging).issubset(_LOGGING_FIELDS):
        raise B10AError("CONFIG_INVALID", "The logging configuration is invalid.")
    return value


def load_config(project_root: Path, data_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load defaults, project overrides, ignored local overrides and env slots."""

    project_path = project_root / "b10a.config.json"
    local_path = data_root / "config" / "local.json"
    project = _read_layer(project_path, source="project", allow_secret_fields=False)
    local = _read_layer(local_path, source="ignored-local", allow_secret_fields=True)
    config = _merge(DEFAULT_CONFIG, project)
    config = _merge(config, local)

    llm = config.setdefault("providers", {}).setdefault("llm-api", {})
    base_url_env = llm.get("base_url_env", "B10A_LLM_API_BASE_URL")
    model_env = llm.get("model_env", "B10A_LLM_MODEL")
    api_key_env = llm.get("api_key_env", "B10A_LLM_API_KEY")
    if isinstance(base_url_env, str) and os.environ.get(base_url_env):
        llm["base_url"] = os.environ[base_url_env]
    if isinstance(model_env, str) and os.environ.get(model_env):
        llm["model"] = os.environ[model_env]
    # Keep the secret out of the merged config entirely. Only the presence and
    # declared source are used by doctor/provider-slot diagnostics.
    llm.pop("api_key", None)
    llm["_secret_present"] = bool(isinstance(api_key_env, str) and os.environ.get(api_key_env)) or bool(
        local.get("providers", {}).get("llm-api", {}).get("api_key")
    )
    llm["_secret_source"] = api_key_env if os.environ.get(api_key_env) else (
        "ignored-local" if llm["_secret_present"] else None
    )
    return config, {
        "project_path": str(project_path),
        "local_path": str(local_path),
        "layers": ["defaults", "project" if project else None, "ignored-local" if local else None],
    }


def config_summary(config: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for provider_id, raw in config.get("providers", {}).items():
        provider = redact(raw)
        provider.pop("_secret_present", None)
        provider.pop("_secret_source", None)
        secret_present = bool(raw.get("_secret_present"))
        secret_source = raw.get("_secret_source")
        provider["configured"] = (
            secret_present and bool(raw.get("base_url"))
            if provider_id == "llm-api"
            else raw.get("type") in {"local", "path-reference-only"}
        )
        provider["secret_present"] = secret_present
        if secret_source:
            provider["secret_source"] = secret_source
        providers[provider_id] = provider
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "layers": metadata["layers"],
        "providers": providers,
    }
