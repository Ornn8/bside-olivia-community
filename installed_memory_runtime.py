"""Normal-install composition for the existing optional Mem0 adapter.

This module owns no memory extraction, retrieval, deduplication, update, or
persistence algorithm. It only validates the installer-owned offline model
cache, selects Mem0's FastEmbed provider, and delegates to ``mem0_memory``.
"""

from __future__ import annotations

from dataclasses import fields
import os
from pathlib import Path
from typing import Mapping

from conversation_memory_port import (
    ConversationMemoryPort,
    UnavailableConversationMemoryPort,
)
from mem0_memory import (
    Mem0Config,
    create_mem0_adapter,
    load_mem0_config,
)
from memory_model import (
    MemoryModelError,
    configure_offline_model_environment,
    load_memory_model_manifest,
    validate_model_cache,
)


_MODEL_MANIFEST = (
    Path(__file__).resolve().parent
    / "installer"
    / "memory-model-manifest.json"
)


class InstalledMem0Config(Mem0Config):
    """Use Mem0's FastEmbed provider while preserving the existing contract."""

    def provider_config(
        self,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        payload = super().provider_config(environ)
        payload["embedder"] = {
            "provider": "fastembed",
            "config": {
                "model": self.embedding_model,
                "embedding_dims": self.embedding_dims,
            },
        }
        return payload


def _installed_config(config: Mem0Config) -> InstalledMem0Config:
    values = {
        field.name: getattr(config, field.name)
        for field in fields(Mem0Config)
    }
    return InstalledMem0Config(**values)


def create_installed_mem0_adapter(
    *,
    environ: Mapping[str, str] | None = None,
    manifest_path: Path | None = None,
) -> ConversationMemoryPort:
    """Create the normal-installed adapter or a stable fail-open port."""

    environment = os.environ if environ is None else environ
    base = load_mem0_config(environ=environment)
    if base.config_error or not base.enabled:
        return create_mem0_adapter(base, environ=environment)
    if not str(environment.get(base.llm_api_key_env, "")).strip():
        return UnavailableConversationMemoryPort(
            "MEM0_LLM_API_KEY_MISSING"
        )

    try:
        manifest = load_memory_model_manifest(
            manifest_path or _MODEL_MANIFEST
        )
    except MemoryModelError as exc:
        return UnavailableConversationMemoryPort(exc.code)
    except (OSError, RuntimeError, TypeError, ValueError):
        return UnavailableConversationMemoryPort(
            "MEMORY_MODEL_MANIFEST_INVALID"
        )

    if (
        manifest.provider != "fastembed"
        or manifest.model != base.embedding_model
        or manifest.dimensions != base.embedding_dims
    ):
        return UnavailableConversationMemoryPort(
            "MEMORY_MODEL_CONFIG_MISMATCH"
        )

    status = validate_model_cache(base.model_cache, manifest)
    if not status.ready:
        return UnavailableConversationMemoryPort(
            status.reason_code or "MEMORY_MODEL_NOT_READY"
        )

    try:
        configure_offline_model_environment(base.model_cache)
        return create_mem0_adapter(
            _installed_config(base),
            environ=environment,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return UnavailableConversationMemoryPort(
            "MEM0_INITIALIZATION_FAILED"
        )


__all__ = [
    "InstalledMem0Config",
    "create_installed_mem0_adapter",
]
