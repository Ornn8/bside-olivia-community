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


def _managed_model(tmp_path: Path, bom) -> ManagedEmbeddingModel:
    return ManagedEmbeddingModel(
        data_root=tmp_path / "data", install_root=tmp_path / "install", bom=bom.model,
        download_root=tmp_path / "install" / "downloads" / "mem0-model",
    )


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
    assert bom.model.sources == (
        "https://modelscope.cn/api/v1/models",
        "https://huggingface.co",
    )
    assert bom.model.source_revisions == (
        "9534737c4ead352e88e6eb6faf4dab9ec1be9eed",
        "7999e1d3359715c523056ef9478215996d62a620",
    )
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
    def __init__(self, payload: bytes, *, status: int, headers=None) -> None:
        super().__init__(payload)
        self.status = status
        self.headers = headers or {}

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
    downloader.last_source = None
    downloader._source_history.clear()
    downloader.opener = lambda *_args, **_kwargs: pytest.fail("network must not be used")
    downloader.download(revision="a" * 40, relative_path="weights.bin", destination=tmp_path / "cached" / "weights.bin")
    assert downloader.last_source == "https://official.example"


def test_model_download_stops_retrying_failed_mirror_for_remaining_files(
    tmp_path: Path,
) -> None:
    payloads = {
        "first.bin": b"first trusted model bytes",
        "second.bin": b"second trusted model bytes",
    }
    artifacts = {
        name: ModelArtifact(len(content), hashlib.sha256(content).hexdigest())
        for name, content in payloads.items()
    }
    observed: list[str] = []

    def opener(request, *, timeout: float):
        assert timeout == 30
        observed.append(request.full_url)
        if request.full_url.startswith("https://mirror.example"):
            raise OSError("synthetic mirror outage")
        return _Response(
            payloads[request.full_url.rsplit("/", 1)[-1]],
            status=200,
        )

    downloader = ResumableModelDownloader(
        repo_id="owner/model",
        revision="a" * 40,
        files=artifacts,
        sources=("https://mirror.example", "https://official.example"),
        download_root=tmp_path / "downloads",
        source_mode="auto",
        pause_requested=threading.Event(),
        progress=lambda *_args: None,
        opener=opener,
    )

    for name in payloads:
        downloader.download(
            revision="a" * 40,
            relative_path=name,
            destination=tmp_path / "stage" / name,
        )

    revision_path = "/owner/model/resolve/" + "a" * 40 + "/"
    assert observed == [
        "https://mirror.example" + revision_path + "first.bin",
        "https://official.example" + revision_path + "first.bin",
        "https://official.example" + revision_path + "second.bin",
    ]
    assert (tmp_path / "stage" / "first.bin").read_bytes() == payloads[
        "first.bin"
    ]
    assert (tmp_path / "stage" / "second.bin").read_bytes() == payloads[
        "second.bin"
    ]


def test_modelscope_fixed_revision_resumes_200_content_range_response(
    tmp_path: Path,
) -> None:
    content = b"trusted model bytes"
    artifact = ModelArtifact(len(content), hashlib.sha256(content).hexdigest())
    partial = tmp_path / "downloads" / "weights.bin.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content[:8])
    partial.with_name(partial.name + ".source").write_text(
        "https://modelscope.cn/api/v1/models", encoding="utf-8"
    )
    observed = []

    def opener(request, *, timeout: float):
        assert timeout == 30
        observed.append((request.full_url, request.headers.get("Range")))
        return _Response(
            content[8:],
            status=200,
            headers={"Content-Range": f"bytes 8-{len(content) - 1}/{len(content)}"},
        )

    downloader = ResumableModelDownloader(
        repo_id="owner/model",
        revision="a" * 40,
        files={"weights.bin": artifact},
        sources=(
            "https://modelscope.cn/api/v1/models",
            "https://huggingface.co",
        ),
        source_revisions=("b" * 40, "a" * 40),
        download_root=tmp_path / "downloads",
        source_mode="auto",
        pause_requested=threading.Event(),
        progress=lambda *_args: None,
        opener=opener,
    )

    destination = tmp_path / "stage" / "weights.bin"
    downloader.download(
        revision="a" * 40,
        relative_path="weights.bin",
        destination=destination,
    )

    assert observed == [(
        "https://modelscope.cn/api/v1/models/owner/model/repo"
        "?Revision=" + "b" * 40 + "&FilePath=weights.bin",
        "bytes=8-",
    )]
    assert destination.read_bytes() == content
    assert downloader.last_source == "https://modelscope.cn/api/v1/models"


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


def test_model_download_resumes_the_source_recorded_by_a_previous_worker(
    tmp_path: Path,
) -> None:
    content = b"trusted model bytes"
    artifact = ModelArtifact(len(content), hashlib.sha256(content).hexdigest())
    partial = tmp_path / "downloads" / "weights.bin.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content[:8])
    partial.with_name(partial.name + ".source").write_text(
        "https://official.example", encoding="utf-8"
    )
    observed: list[tuple[str, str | None]] = []

    def opener(request, *, timeout: float):
        assert timeout == 30
        observed.append((request.full_url, request.headers.get("Range")))
        return _Response(content[8:], status=206)

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

    assert observed == [
        (
            "https://official.example/owner/model/resolve/" + "a" * 40 + "/weights.bin",
            "bytes=8-",
        )
    ]


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
    assert public["schema_version"] == "olivia.capability-status.v2"
    assert public["remaining_bytes"] == 0
    assert public["installed_bytes"] == 0
    assert public["install_locations"] == [
        {
            "root": "installation_root",
            "relative_path": "runtime/mem0-site-packages",
        },
        {
            "root": "local_data_root",
            "relative_path": "memory/model-cache",
        },
    ]
    status_schema = json.loads(
        (CONTRACTS / "mem0_capability_status.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(status_schema).validate(public)
    serialized = json.dumps(public).casefold()
    assert ":\\" not in serialized
    assert "users/" not in serialized


def test_ready_status_reports_actual_managed_disk_usage(tmp_path: Path) -> None:
    runtime = _Layer(ready=True)
    runtime.target = tmp_path / "runtime"
    runtime.target.mkdir()
    (runtime.target / "runtime.bin").write_bytes(b"abc")
    model = _Layer(ready=True)
    model.config = SimpleNamespace(model_cache=tmp_path / "model")
    model.config.model_cache.mkdir()
    (model.config.model_cache / "model.bin").write_bytes(b"12345")
    installer = Mem0CapabilityInstaller(
        runtime=runtime,
        model=model,
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
    )

    assert installer.status().to_dict()["installed_bytes"] == 8


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


def test_background_preflight_failure_becomes_repair() -> None:
    class FailingSecondReadLayer(_Layer):
        def __init__(self) -> None:
            super().__init__()
            self.ready_calls = 0

        def ready(self) -> bool:
            self.ready_calls += 1
            if self.ready_calls == 2:
                raise RuntimeError("synthetic preflight failure")
            return False

    installer = Mem0CapabilityInstaller(
        runtime=FailingSecondReadLayer(),
        model=_Layer(),
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
    )

    assert installer.start(source_mode="auto") == "APPLIED"
    assert installer._thread is not None
    installer._thread.join(timeout=2)

    status = installer.status()
    assert status.state is CapabilityState.REPAIR
    assert status.phase == "queued"
    assert status.reason_code == "MEM0_CAPABILITY_INSTALL_FAILED"


def test_status_reconciles_dead_active_worker_as_repair() -> None:
    installer = Mem0CapabilityInstaller(
        runtime=_Layer(),
        model=_Layer(),
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
    )
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join(timeout=1)
    installer._thread = worker
    installer._status = installer._new_status(
        CapabilityState.DOWNLOADING,
        "runtime",
        5,
        current="python-dependencies",
        source="auto",
    )

    status = installer.status()

    assert status.state is CapabilityState.REPAIR
    assert status.phase == "runtime"
    assert status.downloaded_bytes == 5
    assert status.current_file == "python-dependencies"
    assert status.source == "auto"
    assert status.reason_code == "MEM0_CAPABILITY_INSTALL_FAILED"


def test_status_reconciles_dead_active_worker_as_ready_when_layers_are_ready() -> None:
    installer = Mem0CapabilityInstaller(
        runtime=_Layer(ready=True),
        model=_Layer(ready=True),
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
    )
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join(timeout=1)
    installer._thread = worker
    installer._status = installer._new_status(
        CapabilityState.DOWNLOADING,
        "verification",
        20,
        source="auto",
    )

    status = installer.status()

    assert status.state is CapabilityState.READY
    assert status.phase == "complete"
    assert status.downloaded_bytes == 20
    assert status.reason_code is None


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


def test_resume_keeps_paused_source_and_progress_floor(monkeypatch) -> None:
    class DeferredThread:
        def __init__(self, *, target, kwargs, **_options) -> None:
            self.target = target
            self.kwargs = kwargs

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

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
    installer._status = installer._new_status(
        CapabilityState.PAUSED,
        "model",
        15,
        source="official",
    )

    assert installer.resume(source_mode="auto") == "APPLIED"
    status = installer.status()
    assert status.state is CapabilityState.QUEUED
    assert status.downloaded_bytes == 15
    assert status.source == "official"
    assert installer._thread.kwargs["source_mode"] == "official"


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


def test_managed_runtime_atomically_migrates_legacy_uncompiled_marker_once(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    python_root = install_root / "runtime" / "python-3.12"
    python_root.mkdir(parents=True)
    python_executable = python_root / "python.exe"
    python_executable.write_bytes(b"synthetic")
    target = install_root / "runtime" / "mem0-site-packages"
    (target / "fixture.dist-info").mkdir(parents=True)
    legacy_file = target / "legacy-runtime.txt"
    legacy_file.write_text("preserve until replacement succeeds", encoding="utf-8")
    pth = python_root / "python312._pth"
    pth.write_text(
        f"{target}\n{target / 'win32'}\n{target / 'win32' / 'lib'}\n"
        "python312.zip\nsite-packages\nimport site\n",
        encoding="utf-8",
    )
    requirements = (
        install_root / "local_backend" / "installer" / "mem0-runtime-requirements.txt"
    )
    requirements.parent.mkdir(parents=True)
    requirements.write_bytes(REQUIREMENTS.read_bytes())
    legacy_marker = {
        "requirements_sha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
        "source": "https://pypi.org/simple",
    }
    marker = target / ".olivia-mem0-runtime-manifest.json"
    marker.write_text(json.dumps(legacy_marker), encoding="utf-8")
    attempts: list[list[str]] = []
    fail_install = True

    def runner(
        command, *, environment, pause_requested, progress, progress_roots
    ) -> int:
        del environment, pause_requested, progress_roots
        attempts.append(list(command))
        staging = Path(command[command.index("--target") + 1])
        (staging / "fixture.dist-info").mkdir(parents=True, exist_ok=True)
        progress(1)
        return 1 if fail_install else 0

    def verifier(runtime: Path, requirement_file: Path) -> bool:
        return (
            requirement_file == requirements
            and (runtime / "fixture.dist-info").is_dir()
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

    assert layer.ready() is False
    with pytest.raises(RuntimeError, match="MEM0_RUNTIME_DOWNLOAD_FAILED"):
        layer.install(
            source_mode="official",
            offline_root=None,
            pause_requested=threading.Event(),
            progress=lambda *_args: None,
        )
    assert legacy_file.read_text(encoding="utf-8") == (
        "preserve until replacement succeeds"
    )
    assert json.loads(marker.read_text(encoding="utf-8")) == legacy_marker

    fail_install = False
    layer.install(
        source_mode="official",
        offline_root=None,
        pause_requested=threading.Event(),
        progress=lambda *_args: None,
    )
    assert not legacy_file.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["bytecode_policy"] == (
        "pip-compile-v1"
    )
    assert len(attempts) == 2

    restarted_layer = ManagedMem0Runtime(
        install_root=install_root,
        python_executable=python_executable,
        requirements=requirements,
        sources=(
            "https://pypi.tuna.tsinghua.edu.cn/simple",
            "https://pypi.org/simple",
        ),
        download_bytes=236_253_351,
        verifier=verifier,
        runner=lambda *_args, **_kwargs: pytest.fail(
            "compiled runtime must not run the installer again"
        ),
    )
    restarted_layer.install(
        source_mode="official",
        offline_root=None,
        pause_requested=threading.Event(),
        progress=lambda *_args: pytest.fail(
            "compiled runtime must not report preparation again"
        ),
    )


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

    def runner(
        command, *, environment, pause_requested, progress, progress_roots
    ) -> int:
        assert environment["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
        assert not pause_requested.is_set()
        assert "--ignore-installed" in command
        assert command[command.index("--timeout") + 1] == "15"
        assert command[command.index("--retries") + 1] == "1"
        assert progress_roots
        calls.append(list(command))
        target = Path(command[command.index("--target") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "fixture.dist-info").mkdir(exist_ok=True)
        progress(50_000_000 if len(calls) == 1 else 120_000_000)
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
    progress: list[tuple[int, int, str]] = []

    layer.install(
        source_mode="auto",
        offline_root=None,
        pause_requested=threading.Event(),
        progress=lambda done, total, current: progress.append((done, total, current)),
    )

    assert len(calls) == 2
    assert all("--compile" in command for command in calls)
    assert all("--no-compile" not in command for command in calls)
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
    assert progress[0] == (0, 236_253_351, "python-runtime-preparation")
    assert any(0 < done < total for done, total, _current in progress)
    assert progress[-1] == (
        236_253_351,
        236_253_351,
        "python-runtime-preparation",
    )
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
            "bytecode_policy": "pip-compile-v1",
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
    layer = _managed_model(tmp_path, bom)
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
    layer = _managed_model(tmp_path, bom)
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


def test_managed_embedding_ready_binds_canonical_manifest_to_capability_bom(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bom = load_mem0_capability_bom(MANIFEST, REQUIREMENTS)
    layer = _managed_model(tmp_path, bom)
    manifest = layer.config.model_cache / "olivia-mem0-embedding-manifest.json"
    manifest.parent.mkdir(parents=True)
    files = {name: item.sha256 for name, item in bom.model.files.items()}
    files["model.safetensors"] = "0" * 64
    manifest.write_text(
        json.dumps({"model": bom.model.repo_id, "revision": bom.model.revision, "files": files}),
        encoding="utf-8",
    )
    layer._write_source(bom.model.sources[0])
    monkeypatch.setattr(mem0_memory, "verified_embedding_cache", lambda _config: True)

    assert layer.ready() is False


def test_managed_embedding_migrates_verified_cache_without_source_to_owned_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bom = load_mem0_capability_bom(MANIFEST, REQUIREMENTS)
    layer = _managed_model(tmp_path, bom)
    monkeypatch.setattr(mem0_memory, "verified_embedding_cache", lambda _config: True)
    manifest = layer.config.model_cache / "olivia-mem0-embedding-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "model": bom.model.repo_id,
                "revision": bom.model.revision,
                "files": {
                    name: item.sha256 for name, item in bom.model.files.items()
                },
            }
        ),
        encoding="utf-8",
    )

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


def test_managed_embedding_marks_download_root_before_installer_can_fail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bom = load_mem0_capability_bom(MANIFEST, REQUIREMENTS)
    layer = _managed_model(tmp_path, bom)
    class FailingInstaller:
        def __init__(self, _config, *, downloader, expected_hashes) -> None:
            self.downloader = downloader
            assert expected_hashes == {
                name: item.sha256 for name, item in bom.model.files.items()
            }

        def install(self):
            owner = self.downloader.download_root / ".olivia-mem0-downloads.json"
            assert owner.is_file()
            (self.downloader.download_root / "model.safetensors.part").write_bytes(b"partial")
            return SimpleNamespace(status="REJECTED", reason_code="SYNTHETIC_FAILURE")

    monkeypatch.setattr(mem0_embedding_install, "Mem0EmbeddingInstaller", FailingInstaller)

    with pytest.raises(RuntimeError, match="SYNTHETIC_FAILURE"):
        layer.install(
            source_mode="auto",
            offline_root=None,
            pause_requested=threading.Event(),
            progress=lambda *_args: None,
        )
    layer.uninstall()
    assert not layer.download_root.exists()


def test_capability_migrates_verified_model_before_download_space_preflight() -> None:
    class MigratingLayer(_Layer):
        migration_enabled = False

        def migrate_verified_cache(self) -> bool:
            if not self.migration_enabled:
                return False
            self.is_ready = True
            self.last_source = "verified-existing-cache"
            return True

    runtime = _Layer(ready=True)
    model = MigratingLayer()
    installer = Mem0CapabilityInstaller(
        runtime=runtime,
        model=model,
        version="fixture-v1",
        estimated_download_bytes=20,
        license_summary="fixture licenses",
        requires_gpu=False,
        space_checks=((lambda: 0, 100),),
    )

    assert installer.install(source_mode="auto") == "REJECTED"
    assert installer.status().state is CapabilityState.REPAIR
    model.migration_enabled = True
    assert installer.start(source_mode="auto") == "NOOP"
    assert installer.status().state is CapabilityState.READY


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
