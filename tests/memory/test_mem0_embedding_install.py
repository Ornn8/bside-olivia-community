from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from conversation_memory_port import UnavailableConversationMemoryPort
from mem0_embedding_install import EmbeddingInstallStatus, Mem0EmbeddingInstaller
from mem0_memory import Mem0Config, create_mem0_adapter
from mem0_memory import MEM0_EMBEDDING_MODEL_REVISION
from original_client_companion_backend import OriginalClientCompanionServiceBackend
from original_client_companion_mutation_backend import (
    DirectOriginalClientCompanionMutationBackend,
)
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from conversation_memory_admin import ConversationMemoryAdminService


@dataclass
class SyntheticDownloader:
    files: dict[str, bytes]
    failures: set[str] | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failures = self.failures or set()

    def download(self, *, revision: str, relative_path: str, destination: Path) -> None:
        self.calls.append((revision, relative_path))
        if relative_path in self.failures:
            raise OSError("synthetic download failure")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[relative_path])


def _config(tmp_path: Path) -> Mem0Config:
    return Mem0Config(
        enabled=True,
        data_root=tmp_path / "memory" / "mem0",
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="fixture-model",
        embedding_cache=tmp_path / "model-cache",
    )


def _files() -> dict[str, bytes]:
    return {
        "1_Pooling/config.json": b'{"word_embedding_dimension":512}',
        "config.json": b'{"model_type":"bert"}',
        "config_sentence_transformers.json": b"{}",
        "model.safetensors": b"synthetic weights",
        "modules.json": b"[]",
        "sentence_bert_config.json": b"{}",
        "special_tokens_map.json": b"{}",
        "tokenizer.json": b"{}",
        "tokenizer_config.json": b"{}",
        "vocab.txt": b"synthetic\n",
    }


def test_missing_explicit_install_verifies_then_runtime_reuses_offline_cache(tmp_path: Path) -> None:
    config = _config(tmp_path)
    downloader = SyntheticDownloader(_files())
    installer = Mem0EmbeddingInstaller(config, downloader=downloader)

    assert installer.status().state is EmbeddingInstallStatus.MISSING
    assert isinstance(create_mem0_adapter(config), UnavailableConversationMemoryPort)

    result = installer.install()

    assert result.status == "APPLIED"
    assert installer.status().state is EmbeddingInstallStatus.READY
    assert len(downloader.calls) == len(_files())
    assert {revision for revision, _path in downloader.calls} == {
        MEM0_EMBEDDING_MODEL_REVISION
    }

    captured: dict[str, object] = {}

    def factory(mapping: dict[str, object]):
        captured.update(mapping)
        return object()

    # A fake factory is sufficient: installation must only unblock the existing
    # runtime's local-files-only path, not call a real provider.
    port = create_mem0_adapter(config, memory_factory=factory)
    assert port.config is config  # type: ignore[attr-defined]
    model_kwargs = captured["embedder"]["config"]["model_kwargs"]  # type: ignore[index]
    assert model_kwargs["local_files_only"] is True  # type: ignore[index]


def test_hash_mismatch_never_promotes_ready_cache_and_cleanup_allows_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    files = _files()
    corrupted = dict(files)
    corrupted["model.safetensors"] = b"corrupt"
    installer = Mem0EmbeddingInstaller(
        config,
        downloader=SyntheticDownloader(corrupted),
        expected_hashes={
            name: hashlib.sha256(content).hexdigest() for name, content in files.items()
        },
    )

    failed = installer.install()

    assert failed.status == "REJECTED"
    assert failed.reason_code == "MEM0_EMBEDDING_HASH_MISMATCH"
    assert installer.status().state is EmbeddingInstallStatus.ERROR
    assert not config.embedding_snapshot.exists()
    assert not list(config.model_cache.glob(".olivia-mem0-embedding-stage-*"))

    retry = Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(files))
    assert retry.install().status == "APPLIED"
    assert retry.status().state is EmbeddingInstallStatus.READY


def test_download_fault_cleans_staging_and_concurrent_or_ready_retries_are_idempotent(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    files = _files()
    failed = Mem0EmbeddingInstaller(
        config,
        downloader=SyntheticDownloader(files, failures={"tokenizer.json"}),
    )
    assert failed.install().reason_code == "MEM0_EMBEDDING_INSTALL_FAILED"
    assert not list(config.model_cache.glob(".olivia-mem0-embedding-stage-*"))

    entered = threading.Event()
    release = threading.Event()

    class BlockingDownloader(SyntheticDownloader):
        def download(self, **kwargs: object) -> None:
            if kwargs["relative_path"] == "config.json":
                entered.set()
                assert release.wait(1)
            super().download(**kwargs)  # type: ignore[arg-type]

    downloader = BlockingDownloader(files)
    installer = Mem0EmbeddingInstaller(config, downloader=downloader)
    results: list[str] = []
    first = threading.Thread(target=lambda: results.append(installer.install().status))
    second = threading.Thread(target=lambda: results.append(installer.install().status))
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert sorted(results) == ["APPLIED", "NOOP"]
    assert len(downloader.calls) == len(files)
    assert installer.install().status == "NOOP"


def test_settings_exposes_only_the_confirmed_embedding_action_and_health_is_honest(
    tmp_path: Path,
) -> None:
    assert 'const MEMORY_EMBEDDING_INSTALL_PATH = "/toy/companion/memory/embedding/install";' in BOOTSTRAP_JAVASCRIPT
    assert "MEM0_EMBEDDING_CACHE_UNAVAILABLE" in BOOTSTRAP_JAVASCRIPT
    assert "安装 Embedding" in BOOTSTRAP_JAVASCRIPT
    assert "正在安装 Embedding" in BOOTSTRAP_JAVASCRIPT
    assert "Embedding 安装失败，请重试。" in BOOTSTRAP_JAVASCRIPT
    assert "http://" not in BOOTSTRAP_JAVASCRIPT
    assert "https://" not in BOOTSTRAP_JAVASCRIPT

    memory = UnavailableConversationMemoryPort("MEM0_EMBEDDING_CACHE_UNAVAILABLE")
    admin = ConversationMemoryAdminService(memory, tmp_path / "memory-admin.sqlite3")
    status = OriginalClientCompanionServiceBackend(memory_admin=admin).read_status().to_dict()
    assert status["capabilities"]["memory"] == {
        "state": "unavailable",
        "reason_code": "MEM0_EMBEDDING_CACHE_UNAVAILABLE",
    }

    class RejectingInstaller:
        def install(self):
            from mem0_embedding_install import EmbeddingInstallResult

            return EmbeddingInstallResult("REJECTED", "MEM0_EMBEDDING_HASH_MISMATCH")

    result = DirectOriginalClientCompanionMutationBackend(
        embedding_installer=RejectingInstaller()
    ).install_embedding(
        request_id="request.memory.embedding.install.1",
        reason="synthetic explicit confirmation",
    )
    assert result.status == "REJECTED"
    assert result.reason_code == "MEM0_EMBEDDING_HASH_MISMATCH"
