from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading

from jsonschema import Draft202012Validator
import pytest
import mem0_embedding_install
import mem0_memory

from mem0_capability_install import (
    CapabilityState,
    ManagedMem0Runtime,
    ManagedEmbeddingModel,
    Mem0CapabilityInstaller,
    ModelArtifact,
    ResumableModelDownloader,
    load_mem0_capability_bom,
)


MANIFEST = Path(__file__).parents[2] / "installer" / "mem0-capability-manifest.json"
REQUIREMENTS = Path(__file__).parents[2] / "installer" / "mem0-runtime-requirements.txt"
CONTRACTS = Path(__file__).parents[2] / "contracts"


def test_mem0_bom_closes_runtime_model_hashes_sources_and_license() -> None:
    bom = load_mem0_capability_bom(MANIFEST, REQUIREMENTS)

    assert bom.capability == "long_term_memory"
    assert bom.status == "FIXED"
    assert bom.runtime.package_count == 69
    assert len(bom.runtime.artifacts) == 69
    assert sum(item.size_bytes for item in bom.runtime.artifacts) == 236_253_351
    assert bom.runtime.requirements_sha256 == hashlib.sha256(
        REQUIREMENTS.read_bytes()
    ).hexdigest()
    assert bom.runtime.sources == (
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://pypi.org/simple",
    )
    assert bom.model.repo_id == "BAAI/bge-small-zh-v1.5"
    assert bom.model.revision == "7999e1d3359715c523056ef9478215996d62a620"
    assert len(bom.model.files) == 10
    assert bom.model.files["model.safetensors"] == ModelArtifact(
        size_bytes=95_827_648,
        sha256="354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026",
    )
    assert bom.model.license == "MIT"
    assert bom.requires_gpu is False


def test_mem0_public_manifests_validate_against_versioned_schemas() -> None:
    cases = (
        ("mem0_capability_bom.schema.json", MANIFEST),
        (
            "mem0_runtime_artifacts.schema.json",
            MANIFEST.with_name("mem0-runtime-artifacts.json"),
        ),
    )
    for schema_name, instance_path in cases:
        schema = json.loads((CONTRACTS / schema_name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(
            json.loads(instance_path.read_text(encoding="utf-8"))
        )


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, *, status: int) -> None:
        super().__init__(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_model_download_resumes_partial_file_and_uses_official_fallback(
    tmp_path: Path,
) -> None:
    content = b"trusted model bytes"
    artifact = ModelArtifact(len(content), hashlib.sha256(content).hexdigest())
    partial = tmp_path / "downloads" / "weights.bin.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content[:8])
    partial.with_name(partial.name + ".source").write_text(
        "https://mirror.example", encoding="utf-8"
    )
    observed: list[tuple[str, str | None]] = []

    def opener(request, *, timeout: float):
        assert timeout == 30
        observed.append((request.full_url, request.headers.get("Range")))
        if request.full_url.startswith("https://mirror.example"):
            raise OSError("synthetic mirror outage")
        return _Response(content, status=200)

    progress: list[tuple[int, int, str]] = []
    downloader = ResumableModelDownloader(
        repo_id="owner/model",
        revision="a" * 40,
        files={"weights.bin": artifact},
        sources=("https://mirror.example", "https://official.example"),
        download_root=tmp_path / "downloads",
        source_mode="auto",
        pause_requested=threading.Event(),
        progress=lambda downloaded, total, current: progress.append(
            (downloaded, total, current)
        ),
        opener=opener,
    )
    destination = tmp_path / "stage" / "weights.bin"

    downloader.download(
        revision="a" * 40,
        relative_path="weights.bin",
        destination=destination,
    )

    assert observed == [
        (
            "https://mirror.example/owner/model/resolve/" + "a" * 40 + "/weights.bin",
            "bytes=8-",
        ),
        (
            "https://official.example/owner/model/resolve/" + "a" * 40 + "/weights.bin",
            None,
        ),
    ]
    assert destination.read_bytes() == content
    assert not partial.exists()
    assert progress[-1] == (len(content), len(content), "weights.bin")


def test_model_download_retries_official_when_mirror_payload_hash_is_wrong(
    tmp_path: Path,
) -> None:
    trusted = b"trusted model bytes"
    artifact = ModelArtifact(len(trusted), hashlib.sha256(trusted).hexdigest())
    observed: list[str] = []

    def opener(request, *, timeout: float):
        assert timeout == 30
        observed.append(request.full_url)
        payload = b"x" * len(trusted) if "mirror.example" in request.full_url else trusted
        return _Response(payload, status=200)

    downloader = ResumableModelDownloader(
        repo_id="owner/model",
        revision="a" * 40,
        files={"weights.bin": artifact},
        sources=("https://mirror.example", "https://official.example"),
        download_root=tmp_path / "downloads",
        source_mode="auto",
        pause_requested=threading.Event(),
        progress=lambda *_args: None,
        opener=opener,
    )

    downloader.download(
        revision="a" * 40,
        relative_path="weights.bin",
        destination=tmp_path / "stage" / "weights.bin",
    )

    assert len(observed) == 2
    assert downloader.last_source == "https://official.example"


def test_model_download_discards_partial_when_falling_back_to_another_source(
    tmp_path: Path,
) -> None:
    trusted = b"trusted model bytes"
    artifact = ModelArtifact(len(trusted), hashlib.sha256(trusted).hexdigest())
    observed: list[tuple[str, str | None]] = []

    class InterruptedResponse(_Response):
        def __init__(self) -> None:
            super().__init__(b"bad", status=200)
            self._reads = 0

        def read(self, size: int = -1) -> bytes:
            self._reads += 1
            if self._reads == 1:
                return super().read(3)
            raise OSError("synthetic interrupted mirror")

    def opener(request, *, timeout: float):
        assert timeout == 30
        observed.append((request.full_url, request.headers.get("Range")))
        if "mirror.example" in request.full_url:
            return InterruptedResponse()
        return _Response(trusted, status=200)

    downloader = ResumableModelDownloader(
        repo_id="owner/model",
        revision="a" * 40,
        files={"weights.bin": artifact},
        sources=("https://mirror.example", "https://official.example"),
        download_root=tmp_path / "downloads",
        source_mode="auto",
        pause_requested=threading.Event(),
        progress=lambda *_args: None,
        opener=opener,
    )

    downloader.download(
        revision="a" * 40,
        relative_path="weights.bin",
        destination=tmp_path / "stage" / "weights.bin",
    )

    assert observed[1][1] is None
    assert (tmp_path / "stage" / "weights.bin").read_bytes() == trusted


class _Layer:
    def __init__(self, *, ready: bool = False) -> None:
        self.is_ready = ready
        self.installs: list[str] = []
        self.offline_roots: list[Path | None] = []
        self.uninstalls = 0

    def ready(self) -> bool:
        return self.is_ready

    def install(self, *, source_mode, offline_root, pause_requested, progress) -> None:
        assert not pause_requested.is_set()
        self.installs.append(source_mode)
        self.offline_roots.append(offline_root)
        progress(5, 10, f"{source_mode}.fixture")
        self.is_ready = True

    def uninstall(self) -> None:
        self.uninstalls += 1
        self.is_ready = False


def test_capability_installs_only_after_explicit_start_and_publishes_progress() -> None:
    runtime = _Layer()
    model = _Layer()
    installer = Mem0CapabilityInstaller(
        runtime=runtime,
        model=model,
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
    )

    initial = installer.status()
    assert initial.state is CapabilityState.MISSING
    assert initial.to_dict()["status"] == "UNAVAILABLE"
    assert runtime.installs == []
    assert model.installs == []

    result = installer.install(source_mode="auto")

    assert result == "APPLIED"
    assert runtime.installs == ["auto"]
    assert model.installs == ["auto"]
    ready = installer.status()
    assert ready.state is CapabilityState.READY
    assert ready.version == "fixture-v1"
    assert ready.downloaded_bytes == 20
    assert ready.total_bytes == 20
    public = ready.to_dict()
    assert public["license_summary"] == "fixture licenses"
    assert public["requires_gpu"] is False
    assert public["status"] == "READY"
    status_schema = json.loads(
        (CONTRACTS / "mem0_capability_status.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(status_schema).validate(public)
    assert "path" not in json.dumps(public).casefold()


def test_capability_start_returns_before_background_install_finishes() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingLayer(_Layer):
        def install(self, *, source_mode, offline_root, pause_requested, progress) -> None:
            del offline_root
            self.installs.append(source_mode)
            entered.set()
            assert release.wait(timeout=2)
            assert not pause_requested.is_set()
            progress(1, 1, "fixture")
            self.is_ready = True

    runtime = BlockingLayer()
    model = _Layer()
    installer = Mem0CapabilityInstaller(
        runtime=runtime,
        model=model,
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
    )

    assert installer.start(source_mode="auto") == "APPLIED"
    assert entered.wait(timeout=1)
    assert installer.status().state is CapabilityState.DOWNLOADING
    release.set()
    assert installer._thread is not None
    installer._thread.join(timeout=2)
    assert installer.status().state is CapabilityState.READY


def test_queued_pause_survives_worker_start_and_resume(monkeypatch) -> None:
    class DeferredThread:
        def __init__(self, *, target, kwargs, **_options) -> None:
            self.target = target
            self.kwargs = kwargs

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def run(self) -> None:
            self.target(**self.kwargs)

    monkeypatch.setattr("mem0_capability_install.threading.Thread", DeferredThread)
    runtime = _Layer()
    model = _Layer()
    installer = Mem0CapabilityInstaller(
        runtime=runtime,
        model=model,
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
    )
    assert installer.start(source_mode="auto") == "APPLIED"
    assert installer.pause() == "APPLIED"
    installer._thread.run()
    assert installer.status().state is CapabilityState.PAUSED
    assert runtime.installs == []

    assert installer.resume(source_mode="auto") == "APPLIED"
    installer._thread.run()
    assert runtime.installs == ["auto"]
    assert model.installs == ["auto"]
    assert installer.status().state is CapabilityState.READY


def test_capability_pause_resume_and_uninstall_keep_model_unless_confirmed() -> None:
    runtime = _Layer(ready=True)
    model = _Layer(ready=True)
    installer = Mem0CapabilityInstaller(
        runtime=runtime,
        model=model,
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
    )

    assert installer.pause() == "NOOP"
    assert installer.resume(source_mode="official") == "NOOP"
    assert installer.uninstall(remove_model=False) == "APPLIED"
    assert runtime.uninstalls == 1
    assert model.uninstalls == 0
    assert installer.status().state is CapabilityState.MISSING

    runtime.is_ready = True
    assert installer.uninstall(remove_model=True) == "APPLIED"
    assert runtime.uninstalls == 2
    assert model.uninstalls == 1


def test_capability_uninstall_reports_failure_instead_of_missing() -> None:
    class FailingLayer(_Layer):
        def uninstall(self) -> None:
            raise OSError("synthetic lock")

    installer = Mem0CapabilityInstaller(
        runtime=FailingLayer(ready=True),
        model=_Layer(ready=True),
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
    )

    assert installer.uninstall(remove_model=False) == "REJECTED"
    status = installer.status()
    assert status.state is CapabilityState.REPAIR
    assert status.reason_code == "MEM0_CAPABILITY_UNINSTALL_FAILED"
    assert status.to_dict()["status"] == "UNAVAILABLE"


def test_ready_status_fails_closed_when_live_layer_is_lost() -> None:
    runtime, model = _Layer(ready=True), _Layer(ready=True)
    installer = Mem0CapabilityInstaller(
        runtime=runtime, model=model, version="v1", estimated_download_bytes=20,
        license_summary="licenses", requires_gpu=False,
    )
    assert installer.status().state is CapabilityState.READY
    model.is_ready = False
    status = installer.status()
    assert status.state is CapabilityState.REPAIR
    assert status.reason_code == "MEM0_CAPABILITY_VERIFY_FAILED"


def test_progress_uses_bom_weights_and_actual_transport_source() -> None:
    runtime, model = _Layer(), _Layer()
    runtime.last_source = "https://official.example/simple"
    installer = Mem0CapabilityInstaller(
        runtime=runtime, model=model, version="v1", estimated_download_bytes=30,
        runtime_download_bytes=10, license_summary="licenses", requires_gpu=False,
    )
    installer._progress("runtime", runtime, "auto")(5, 10, "wheel")
    assert installer.status().downloaded_bytes == 5
    assert installer.status().source == runtime.last_source
    installer._progress("model", model, "auto")(5, 10, "model")
    assert installer.status().downloaded_bytes == 20


def test_managed_runtime_uses_mirror_then_official_and_registers_atomic_target(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    python_root = install_root / "runtime" / "python-3.12"
    python_root.mkdir(parents=True)
    python_executable = python_root / "python.exe"
    python_executable.write_bytes(b"synthetic")
    pth = python_root / "python312._pth"
    pth.write_text("python312.zip\nsite-packages\nimport site\n", encoding="utf-8")
    requirements = install_root / "local_backend" / "installer" / "mem0-runtime-requirements.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_bytes(REQUIREMENTS.read_bytes())
    calls: list[list[str]] = []

    def runner(command, *, environment, pause_requested) -> int:
        assert environment["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
        assert not pause_requested.is_set()
        calls.append(list(command))
        target = Path(command[command.index("--target") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "fixture.dist-info").mkdir(exist_ok=True)
        return 1 if len(calls) == 1 else 0

    def verifier(runtime: Path, requirement_file: Path) -> bool:
        manifest = runtime / ".olivia-mem0-runtime-manifest.json"
        return (
            requirement_file == requirements
            and (runtime / "fixture.dist-info").is_dir()
            and manifest.is_file()
        )

    layer = ManagedMem0Runtime(
        install_root=install_root,
        python_executable=python_executable,
        requirements=requirements,
        sources=(
            "https://pypi.tuna.tsinghua.edu.cn/simple",
            "https://pypi.org/simple",
        ),
        download_bytes=236_253_351,
        verifier=verifier,
        runner=runner,
    )
    progress: list[str] = []

    layer.install(
        source_mode="auto",
        offline_root=None,
        pause_requested=threading.Event(),
        progress=lambda _done, _total, current: progress.append(current),
    )

    assert len(calls) == 2
    assert "https://pypi.tuna.tsinghua.edu.cn/simple" in calls[0]
    assert "https://pypi.org/simple" in calls[1]
    assert layer.ready() is True
    assert layer.last_source == "https://pypi.org/simple"
    registered = pth.read_text(encoding="utf-8").splitlines()
    target = install_root / "runtime" / "mem0-site-packages"
    assert registered[:3] == [
        str(target),
        str(target / "win32"),
        str(target / "win32" / "lib"),
    ]
    assert progress == [
        "python-dependencies",
        "python-dependencies",
        "python-dependencies",
    ]
    marker = target / ".olivia-mem0-runtime-manifest.json"
    owned = marker.read_bytes()
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="MEM0_RUNTIME_OWNERSHIP_INVALID"):
        layer.uninstall()
    assert target.exists()
    marker.write_bytes(owned)
    layer.uninstall()
    assert not target.exists()
    assert all("mem0-site-packages" not in line for line in pth.read_text(encoding="utf-8").splitlines())


def test_managed_runtime_verifies_in_embedded_python_child_and_caches_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_root = tmp_path / "install"
    python_root = install_root / "runtime" / "python-3.12"
    python_root.mkdir(parents=True)
    python_executable = python_root / "python.exe"
    python_executable.write_bytes(b"synthetic")
    pth = python_root / "python312._pth"
    target = install_root / "runtime" / "mem0-site-packages"
    target.mkdir(parents=True)
    pth.write_text(
        f"{target}\n{target / 'win32'}\n{target / 'win32' / 'lib'}\n"
        "python312.zip\nsite-packages\nimport site\n",
        encoding="utf-8",
    )
    requirements = install_root / "local_backend" / "installer" / "mem0-runtime-requirements.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_bytes(REQUIREMENTS.read_bytes())
    (target / ".olivia-mem0-runtime-manifest.json").write_text(
        json.dumps({
            "requirements_sha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
            "source": "https://official.example/simple",
        }), encoding="utf-8",
    )
    calls: list[list[str]] = []

    def run(command, **options):
        calls.append(list(command))
        assert options["stdout"] is not None
        assert options["stderr"] is not None
        assert options["timeout"] == 120
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("mem0_capability_install.subprocess.run", run)
    layer = ManagedMem0Runtime(
        install_root=install_root,
        python_executable=python_executable,
        requirements=requirements,
        sources=("https://mirror.example/simple", "https://official.example/simple"),
        download_bytes=236_253_351,
    )

    assert layer.ready() is True
    assert layer.ready() is True
    assert len(calls) == 1
    assert calls[0][0] == str(python_executable)
    assert calls[0][-2:] == [str(target), str(requirements)]


def test_managed_embedding_uninstall_removes_model_and_transport_cache(
    tmp_path: Path,
) -> None:
    bom = load_mem0_capability_bom(MANIFEST, REQUIREMENTS)
    layer = ManagedEmbeddingModel(
        data_root=tmp_path / "data",
        install_root=tmp_path / "install",
        bom=bom.model,
        download_root=tmp_path / "install" / "downloads" / "mem0-model",
    )
    layer.config.embedding_snapshot.mkdir(parents=True)
    manifest = layer.config.model_cache / "olivia-mem0-embedding-manifest.json"
    manifest.write_text(
        json.dumps({
            "model": bom.model.repo_id,
            "revision": bom.model.revision,
            "files": {name: item.sha256 for name, item in bom.model.files.items()},
        }),
        encoding="utf-8",
    )
    layer._write_source(bom.model.sources[0])
    layer.download_root.mkdir(parents=True)
    (layer.download_root / "model.safetensors.part").write_bytes(b"partial")
    (layer.download_root / ".olivia-mem0-downloads.json").write_text(
        json.dumps({"repo_id": bom.model.repo_id, "revision": bom.model.revision}),
        encoding="utf-8",
    )

    layer.uninstall()

    assert not layer.config.embedding_snapshot.exists()
    assert not manifest.exists()
    assert not layer.download_root.exists()


def test_managed_embedding_keeps_canonical_manifest_and_persists_source_separately(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bom = load_mem0_capability_bom(MANIFEST, REQUIREMENTS)
    layer = ManagedEmbeddingModel(
        data_root=tmp_path / "data",
        install_root=tmp_path / "install",
        bom=bom.model,
        download_root=tmp_path / "install" / "downloads" / "mem0-model",
    )
    expected = {name: item.sha256 for name, item in bom.model.files.items()}

    def verified(config) -> bool:
        try:
            payload = json.loads(
                (config.model_cache / "olivia-mem0-embedding-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        except OSError:
            return False
        return set(payload) == {"model", "revision", "files"} and payload["files"] == expected

    class FakeInstaller:
        def __init__(self, config, *, downloader, expected_hashes) -> None:
            self.config = config
            self.downloader = downloader
            assert expected_hashes == expected

        def install(self):
            self.downloader.last_source = bom.model.sources[0]
            self.config.model_cache.mkdir(parents=True, exist_ok=True)
            manifest = self.config.model_cache / "olivia-mem0-embedding-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "model": bom.model.repo_id,
                        "revision": bom.model.revision,
                        "files": expected,
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(status="APPLIED", reason_code=None)

    monkeypatch.setattr(mem0_memory, "verified_embedding_cache", verified)
    monkeypatch.setattr(mem0_embedding_install, "Mem0EmbeddingInstaller", FakeInstaller)

    layer.install(
        source_mode="auto",
        offline_root=None,
        pause_requested=threading.Event(),
        progress=lambda *_args: None,
    )

    manifest = json.loads(
        (layer.config.model_cache / "olivia-mem0-embedding-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(manifest) == {"model", "revision", "files"}
    assert layer.ready() is True
    assert layer.last_source == bom.model.sources[0]


def test_managed_embedding_migrates_verified_cache_without_source_to_owned_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bom = load_mem0_capability_bom(MANIFEST, REQUIREMENTS)
    layer = ManagedEmbeddingModel(
        data_root=tmp_path / "data",
        install_root=tmp_path / "install",
        bom=bom.model,
        download_root=tmp_path / "install" / "downloads" / "mem0-model",
    )
    monkeypatch.setattr(mem0_memory, "verified_embedding_cache", lambda _config: True)

    class ExistingCacheInstaller:
        def __init__(self, _config, *, downloader, expected_hashes) -> None:
            del downloader
            assert expected_hashes == {
                name: item.sha256 for name, item in bom.model.files.items()
            }

        def install(self):
            return SimpleNamespace(status="NOOP", reason_code=None)

    monkeypatch.setattr(
        mem0_embedding_install, "Mem0EmbeddingInstaller", ExistingCacheInstaller
    )

    layer.install(
        source_mode="auto",
        offline_root=None,
        pause_requested=threading.Event(),
        progress=lambda *_args: None,
    )

    assert layer.ready() is True
    assert layer.last_source == "verified-existing-cache"


def test_capability_rejects_start_before_thread_when_disk_space_is_low() -> None:
    runtime = _Layer()
    model = _Layer()
    installer = Mem0CapabilityInstaller(
        runtime=runtime,
        model=model,
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
        space_checks=((lambda: 100, 100), (lambda: 99, 100)),
    )

    assert installer.install(source_mode="auto") == "REJECTED"
    status = installer.status()
    assert status.state is CapabilityState.REPAIR
    assert status.reason_code == "MEM0_CAPABILITY_DISK_SPACE_LOW"
    assert runtime.installs == []
    assert model.installs == []
