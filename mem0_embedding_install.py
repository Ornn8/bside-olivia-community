"""Explicit, verified installation of the optional local Mem0 embedding.

The running Mem0 adapter remains offline-only.  This module is the one narrow
place allowed to download the pinned public model after a user confirms the
action in the original Olivia Settings surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import uuid
from typing import Protocol

from mem0_memory import (
    MEM0_EMBEDDING_MODEL,
    MEM0_EMBEDDING_MODEL_REVISION,
    Mem0Config,
    verified_embedding_cache,
)


_MANIFEST_NAME = "olivia-mem0-embedding-manifest.json"
_STAGE_PREFIX = ".olivia-mem0-embedding-stage-"
_INSTALL_LOCKS: dict[str, threading.Lock] = {}
_INSTALL_LOCKS_GUARD = threading.Lock()


class EmbeddingInstallStatus(StrEnum):
    MISSING = "missing"
    INSTALLING = "installing"
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True)
class EmbeddingInstallState:
    state: EmbeddingInstallStatus
    reason_code: str | None = None


@dataclass(frozen=True)
class EmbeddingInstallResult:
    status: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"APPLIED", "NOOP", "REJECTED"}:
            raise ValueError("embedding install result status is invalid")
        if self.reason_code is not None and not self.reason_code.startswith("MEM0_"):
            raise ValueError("embedding install result reason is invalid")


class EmbeddingDownloader(Protocol):
    def download(
        self,
        *,
        revision: str,
        relative_path: str,
        destination: Path,
    ) -> None: ...


class HuggingFaceEmbeddingDownloader:
    """Anonymous per-file downloader used only after explicit confirmation."""

    def download(
        self,
        *,
        revision: str,
        relative_path: str,
        destination: Path,
    ) -> None:
        try:
            from huggingface_hub import hf_hub_download
        except ImportError as exc:
            raise RuntimeError("MEM0_EMBEDDING_DOWNLOADER_UNAVAILABLE") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        local_root = destination
        for _ in relative_path.split("/"):
            local_root = local_root.parent
        fetched = hf_hub_download(
            repo_id=MEM0_EMBEDDING_MODEL,
            filename=relative_path,
            revision=revision,
            local_dir=str(local_root),
            local_dir_use_symlinks=False,
            token=False,
        )
        fetched_path = Path(fetched)
        if fetched_path.resolve() != destination.resolve():
            shutil.copyfile(fetched_path, destination)


def _install_lock(cache_root: Path) -> threading.Lock:
    key = str(cache_root.resolve()).casefold()
    with _INSTALL_LOCKS_GUARD:
        return _INSTALL_LOCKS.setdefault(key, threading.Lock())


def _stage_config(config: Mem0Config, stage_root: Path) -> Mem0Config:
    return Mem0Config(
        enabled=config.enabled,
        data_root=config.data_root,
        user_id=config.user_id,
        agent_id=config.agent_id,
        collection_name=config.collection_name,
        llm_base_url=config.llm_base_url,
        llm_model=config.llm_model,
        llm_api_key_env=config.llm_api_key_env,
        embedding_model=config.embedding_model,
        embedding_dims=config.embedding_dims,
        embedding_cache=stage_root,
        context_max_chars=config.context_max_chars,
        config_error=config.config_error,
        write_timeout_seconds=config.write_timeout_seconds,
        search_timeout_seconds=config.search_timeout_seconds,
        outbox_data_root=config.outbox_data_root,
        outbox_enabled=config.outbox_enabled,
        outbox_interval_seconds=config.outbox_interval_seconds,
    )


class Mem0EmbeddingInstaller:
    """Install exactly one pinned embedding into the existing cache contract."""

    def __init__(
        self,
        config: Mem0Config,
        *,
        downloader: EmbeddingDownloader | None = None,
        expected_hashes: dict[str, str] | None = None,
    ) -> None:
        if not isinstance(config, Mem0Config):
            raise TypeError("a Mem0 config is required")
        if config.embedding_model != MEM0_EMBEDDING_MODEL:
            raise ValueError("the pinned embedding model is required")
        if expected_hashes is not None and set(expected_hashes) != set(_snapshot_files()):
            raise ValueError("expected embedding hashes are incomplete")
        self.config = config
        self.downloader = downloader or HuggingFaceEmbeddingDownloader()
        self.expected_hashes = expected_hashes
        self._state = EmbeddingInstallState(EmbeddingInstallStatus.MISSING)
        self._state_lock = threading.Lock()

    def status(self) -> EmbeddingInstallState:
        if verified_embedding_cache(self.config):
            return EmbeddingInstallState(EmbeddingInstallStatus.READY)
        with self._state_lock:
            return self._state

    def install(self) -> EmbeddingInstallResult:
        lock = _install_lock(self.config.model_cache)
        with lock:
            if verified_embedding_cache(self.config):
                self._set_state(EmbeddingInstallStatus.READY)
                return EmbeddingInstallResult("NOOP")
            self._set_state(EmbeddingInstallStatus.INSTALLING)
            stage_root = self.config.model_cache / f"{_STAGE_PREFIX}{uuid.uuid4().hex}"
            try:
                staged = _stage_config(self.config, stage_root)
                hashes = self._download_and_hash(staged)
                self._write_manifest(stage_root, hashes)
                if not verified_embedding_cache(staged):
                    raise _InstallFailure("MEM0_EMBEDDING_VERIFICATION_FAILED")
                self._promote(stage_root, staged)
                if not verified_embedding_cache(self.config):
                    raise _InstallFailure("MEM0_EMBEDDING_VERIFICATION_FAILED")
            except _InstallFailure as exc:
                self._set_state(EmbeddingInstallStatus.ERROR, exc.code)
                return EmbeddingInstallResult("REJECTED", exc.code)
            except (OSError, RuntimeError, TypeError, ValueError):
                self._set_state(EmbeddingInstallStatus.ERROR, "MEM0_EMBEDDING_INSTALL_FAILED")
                return EmbeddingInstallResult("REJECTED", "MEM0_EMBEDDING_INSTALL_FAILED")
            finally:
                shutil.rmtree(stage_root, ignore_errors=True)
            self._set_state(EmbeddingInstallStatus.READY)
            return EmbeddingInstallResult("APPLIED")

    def _download_and_hash(self, staged: Mem0Config) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for relative_path in sorted(_snapshot_files()):
            destination = staged.embedding_snapshot.joinpath(*relative_path.split("/"))
            self.downloader.download(
                revision=MEM0_EMBEDDING_MODEL_REVISION,
                relative_path=relative_path,
                destination=destination,
            )
            try:
                with destination.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
            except OSError as exc:
                raise _InstallFailure("MEM0_EMBEDDING_INSTALL_FAILED") from exc
            expected = self.expected_hashes.get(relative_path) if self.expected_hashes else None
            if expected is not None and digest != expected:
                raise _InstallFailure("MEM0_EMBEDDING_HASH_MISMATCH")
            hashes[relative_path] = digest
        return hashes

    def _write_manifest(self, stage_root: Path, hashes: dict[str, str]) -> None:
        (stage_root / _MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "model": MEM0_EMBEDDING_MODEL,
                    "revision": MEM0_EMBEDDING_MODEL_REVISION,
                    "files": hashes,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def _promote(self, stage_root: Path, staged: Mem0Config) -> None:
        target = self.config.embedding_snapshot
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged.embedding_snapshot, target)
        os.replace(stage_root / _MANIFEST_NAME, self.config.model_cache / _MANIFEST_NAME)

    def _set_state(self, state: EmbeddingInstallStatus, reason_code: str | None = None) -> None:
        with self._state_lock:
            self._state = EmbeddingInstallState(state, reason_code)


class _InstallFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _snapshot_files() -> frozenset[str]:
    # Kept in one place: this is the exact #127 file/manifest contract.
    from mem0_memory import embedding_snapshot_files

    return embedding_snapshot_files()


__all__ = [
    "EmbeddingDownloader",
    "EmbeddingInstallResult",
    "EmbeddingInstallState",
    "EmbeddingInstallStatus",
    "HuggingFaceEmbeddingDownloader",
    "Mem0EmbeddingInstaller",
]
