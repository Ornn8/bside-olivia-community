from __future__ import annotations

import hashlib
import inspect
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import mem0_embedding_install as install_module
from conversation_memory_port import UnavailableConversationMemoryPort
from mem0_embedding_install import (
    EmbeddingInstallStatus,
    HuggingFaceEmbeddingDownloader,
    Mem0EmbeddingInstaller,
)
from mem0_memory import Mem0Config, create_mem0_adapter, verified_embedding_cache
from mem0_memory import MEM0_EMBEDDING_MODEL_REVISION
from original_client_companion_backend import OriginalClientCompanionServiceBackend
from original_client_companion_mutation_backend import (
    DirectOriginalClientCompanionMutationBackend,
)
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from conversation_memory_admin import ConversationMemoryAdminService
from original_client_companion_api import CompanionReadStatus


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


def test_manifest_promotion_failure_restores_the_previously_damaged_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    files = _files()
    assert Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(files)).install().status == "APPLIED"
    (config.embedding_snapshot / "config.json").write_bytes(b"damaged")
    assert not verified_embedding_cache(config)

    replace = install_module.os.replace

    def fail_manifest(source: Path | str, destination: Path | str) -> None:
        if (
            Path(source).name == "olivia-mem0-embedding-manifest.json"
            and Path(source).parent.name.startswith(".olivia-mem0-embedding-stage-")
        ):
            raise OSError("synthetic manifest promotion failure")
        replace(source, destination)

    monkeypatch.setattr(install_module.os, "replace", fail_manifest)

    failed = Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(files)).install()

    assert failed.status == "REJECTED"
    assert not verified_embedding_cache(config)


def test_snapshot_backup_fault_preserves_a_previously_ready_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    files = _files()
    assert Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(files)).install().status == "APPLIED"
    snapshot_before = (config.embedding_snapshot / "config.json").read_bytes()
    manifest_before = (config.model_cache / "olivia-mem0-embedding-manifest.json").read_bytes()

    verifier = install_module.verified_embedding_cache

    def force_one_reinstall(candidate: Mem0Config) -> bool:
        if candidate is config:
            monkeypatch.setattr(install_module, "verified_embedding_cache", verifier)
            return False
        return verifier(candidate)

    monkeypatch.setattr(install_module, "verified_embedding_cache", force_one_reinstall)
    replace = install_module.os.replace

    def fail_snapshot_backup(source: Path | str, destination: Path | str) -> None:
        if Path(source) == config.embedding_snapshot and Path(destination).name == "rollback-model":
            raise OSError("synthetic snapshot backup failure")
        replace(source, destination)

    monkeypatch.setattr(install_module.os, "replace", fail_snapshot_backup)

    failed = Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(files)).install()

    assert failed.status == "REJECTED"
    assert verified_embedding_cache(config)
    assert (config.embedding_snapshot / "config.json").read_bytes() == snapshot_before
    assert (config.model_cache / "olivia-mem0-embedding-manifest.json").read_bytes() == manifest_before
    assert not list(config.model_cache.glob(".olivia-mem0-embedding-stage-*"))


def test_manifest_backup_fault_preserves_a_previously_ready_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    files = _files()
    assert Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(files)).install().status == "APPLIED"
    snapshot_before = (config.embedding_snapshot / "config.json").read_bytes()
    manifest_before = (config.model_cache / "olivia-mem0-embedding-manifest.json").read_bytes()

    verifier = install_module.verified_embedding_cache

    def force_one_reinstall(candidate: Mem0Config) -> bool:
        if candidate is config:
            monkeypatch.setattr(install_module, "verified_embedding_cache", verifier)
            return False
        return verifier(candidate)

    monkeypatch.setattr(install_module, "verified_embedding_cache", force_one_reinstall)
    replace = install_module.os.replace

    def fail_manifest_backup(source: Path | str, destination: Path | str) -> None:
        if Path(source).name == "olivia-mem0-embedding-manifest.json" and Path(destination).name == "rollback-manifest.json":
            raise OSError("synthetic manifest backup failure")
        replace(source, destination)

    monkeypatch.setattr(install_module.os, "replace", fail_manifest_backup)

    failed = Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(files)).install()

    assert failed.status == "REJECTED"
    assert verified_embedding_cache(config)
    assert (config.embedding_snapshot / "config.json").read_bytes() == snapshot_before
    assert (config.model_cache / "olivia-mem0-embedding-manifest.json").read_bytes() == manifest_before
    assert not list(config.model_cache.glob(".olivia-mem0-embedding-stage-*"))


@pytest.mark.parametrize("failed_name", ["snapshot", "manifest"])
def test_replace_failures_leave_a_new_cache_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_name: str
) -> None:
    config = _config(tmp_path)
    replace = install_module.os.replace

    def fail_replace(source: Path | str, destination: Path | str) -> None:
        is_stage = any(
            parent.name.startswith(".olivia-mem0-embedding-stage-")
            for parent in Path(source).parents
        )
        is_snapshot = Path(source).name == MEM0_EMBEDDING_MODEL_REVISION
        is_manifest = Path(source).name == "olivia-mem0-embedding-manifest.json"
        if is_stage and (
            (failed_name == "snapshot" and is_snapshot)
            or (failed_name == "manifest" and is_manifest)
        ):
            raise OSError(f"synthetic {failed_name} promotion failure")
        replace(source, destination)

    monkeypatch.setattr(install_module.os, "replace", fail_replace)

    failed = Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(_files())).install()

    assert failed.status == "REJECTED"
    assert not config.embedding_snapshot.exists()
    assert not verified_embedding_cache(config)
    assert not list(config.model_cache.glob(".olivia-mem0-embedding-stage-*"))


@pytest.mark.parametrize("verifier_outcome", [False, RuntimeError("synthetic verifier failure")])
def test_post_promotion_verifier_failure_restores_the_previously_damaged_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verifier_outcome: bool | RuntimeError
) -> None:
    config = _config(tmp_path)
    files = _files()
    assert Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(files)).install().status == "APPLIED"
    (config.embedding_snapshot / "config.json").write_bytes(b"damaged")
    manifest_before = (config.model_cache / "olivia-mem0-embedding-manifest.json").read_bytes()
    assert not verified_embedding_cache(config)

    verifier = install_module.verified_embedding_cache
    config_checks = 0

    def fail_after_promotion(candidate: Mem0Config) -> bool:
        nonlocal config_checks
        if candidate is config:
            config_checks += 1
            if config_checks == 2:
                if isinstance(verifier_outcome, Exception):
                    raise verifier_outcome
                return verifier_outcome
        return verifier(candidate)

    monkeypatch.setattr(install_module, "verified_embedding_cache", fail_after_promotion)

    failed = Mem0EmbeddingInstaller(config, downloader=SyntheticDownloader(files)).install()

    assert failed.status == "REJECTED"
    assert not verified_embedding_cache(config)
    assert (config.embedding_snapshot / "config.json").read_bytes() == b"damaged"
    assert (config.model_cache / "olivia-mem0-embedding-manifest.json").read_bytes() == manifest_before
    assert not list(config.model_cache.glob(".olivia-mem0-embedding-stage-*"))


def test_read_and_mutation_share_fast_background_install_state(tmp_path: Path) -> None:
    class SharedInstaller:
        def __init__(self) -> None:
            self.state = EmbeddingInstallStatus.MISSING
            self.start_calls = 0

        def status(self):
            from mem0_embedding_install import EmbeddingInstallState

            return EmbeddingInstallState(self.state)

        def start(self):
            from mem0_embedding_install import EmbeddingInstallResult

            self.start_calls += 1
            self.state = EmbeddingInstallStatus.INSTALLING
            return EmbeddingInstallResult("APPLIED")

    memory = UnavailableConversationMemoryPort("MEM0_EMBEDDING_CACHE_UNAVAILABLE")
    admin = ConversationMemoryAdminService(memory, tmp_path / "memory-admin.sqlite3")
    installer = SharedInstaller()
    read = OriginalClientCompanionServiceBackend(
        memory_admin=admin,
        embedding_installer=installer,
    )
    mutate = DirectOriginalClientCompanionMutationBackend(embedding_installer=installer)

    before = read.read_status()
    assert isinstance(before, CompanionReadStatus)
    before_payload = before.to_dict()
    assert before_payload["capabilities"]["memory"]["embedding"] == {
        "state": "missing"
    }
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parents[2] / "contracts" / "original_client_memory_lifecycle.schema.json").read_text("utf-8")
    )
    assert not list(Draft202012Validator(schema).iter_errors(before_payload))

    accepted = mutate.install_embedding(
        request_id="request.memory.embedding.install.1",
        reason="synthetic explicit confirmation",
    )

    assert accepted.status == "APPLIED"
    assert installer.start_calls == 1
    assert read.read_status().to_dict()["capabilities"]["memory"]["embedding"] == {
        "state": "installing"
    }


def test_schema_allows_embedding_only_under_memory() -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parents[2] / "contracts" / "original_client_memory_lifecycle.schema.json").read_text("utf-8")
    )
    validator = Draft202012Validator(schema)

    def payload() -> dict[str, object]:
        return {
            "schema_version": "p03.original-companion-read.v1",
            "status": "READY",
            "capabilities": {
                "memory": {"state": "unavailable"},
                "private_world": {"state": "available"},
                "candidates": {"state": "available"},
            },
        }

    for state in ("missing", "installing", "ready", "error"):
        candidate = payload()
        candidate["capabilities"]["memory"]["embedding"] = {"state": state}  # type: ignore[index]
        assert not list(validator.iter_errors(candidate))

    for capability in ("private_world", "candidates"):
        candidate = payload()
        candidate["capabilities"][capability]["embedding"] = {"state": "ready"}  # type: ignore[index]
        assert list(validator.iter_errors(candidate))


def test_start_returns_before_download_and_reuses_one_background_worker(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    class BlockingDownloader(SyntheticDownloader):
        def download(self, **kwargs: object) -> None:
            if kwargs["relative_path"] == "config.json":
                entered.set()
                assert release.wait(1)
            super().download(**kwargs)  # type: ignore[arg-type]
            if kwargs["relative_path"] == "vocab.txt":
                completed.set()

    downloader = BlockingDownloader(_files())
    installer = Mem0EmbeddingInstaller(_config(tmp_path), downloader=downloader)

    assert installer.start().status == "APPLIED"
    assert entered.wait(1)
    assert installer.status().state is EmbeddingInstallStatus.INSTALLING
    assert installer.start().status == "APPLIED"
    assert [path for _revision, path in downloader.calls] == ["1_Pooling/config.json"]

    release.set()
    assert completed.wait(1)
    deadline = time.monotonic() + 1
    while (
        installer.status().state is EmbeddingInstallStatus.INSTALLING
        and time.monotonic() < deadline
    ):
        threading.Event().wait(0.01)
    assert installer.status().state is EmbeddingInstallStatus.READY
    assert len(downloader.calls) == len(_files())


def test_huggingface_download_call_shape_uses_only_supported_pinned_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def hf_hub_download(
        *, repo_id: str, filename: str, revision: str, local_dir: str, token: bool
    ) -> str:
        observed.update(
            repo_id=repo_id,
            filename=filename,
            revision=revision,
            local_dir=local_dir,
            token=token,
        )
        destination = Path(local_dir) / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"synthetic")
        return str(destination)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(hf_hub_download=hf_hub_download),
    )
    destination = tmp_path / "stage" / "1_Pooling" / "config.json"

    HuggingFaceEmbeddingDownloader().download(
        revision=MEM0_EMBEDDING_MODEL_REVISION,
        relative_path="1_Pooling/config.json",
        destination=destination,
    )

    inspect.signature(hf_hub_download).bind(**observed)
    assert observed["revision"] == MEM0_EMBEDDING_MODEL_REVISION
    assert observed["token"] is False
    assert destination.read_bytes() == b"synthetic"


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
    assert "capability.embedding" in BOOTSTRAP_JAVASCRIPT
    assert "window.setTimeout(refresh, 1000);" in BOOTSTRAP_JAVASCRIPT
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
        def start(self):
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
