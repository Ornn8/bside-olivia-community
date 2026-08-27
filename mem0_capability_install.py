"""Verified, user-triggered installation of the optional Mem0 capability."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Protocol
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import uuid

from installer.uninstall_safety import safe_managed_target


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
Progress = Callable[[int, int, str], None]


class CapabilityState(StrEnum):
    MISSING = "missing"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    READY = "ready"
    PAUSED = "paused"
    REPAIR = "repair"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class ModelArtifact:
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if self.size_bytes < 0 or not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("model artifact is invalid")


@dataclass(frozen=True)
class RuntimeArtifact:
    filename: str
    size_bytes: int
    sha256: str
    license: str


@dataclass(frozen=True)
class RuntimeBOM:
    requirements_sha256: str
    package_count: int
    estimated_download_bytes: int
    sources: tuple[str, str]
    artifacts: tuple[RuntimeArtifact, ...]


@dataclass(frozen=True)
class ModelBOM:
    repo_id: str
    revision: str
    license: str
    sources: tuple[str, str]
    files: Mapping[str, ModelArtifact]

    @property
    def download_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files.values())


@dataclass(frozen=True)
class Mem0CapabilityBOM:
    capability: str
    status: str
    version: str
    runtime: RuntimeBOM
    model: ModelBOM
    license_summary: str
    requires_gpu: bool

    @property
    def estimated_download_bytes(self) -> int:
        return self.runtime.estimated_download_bytes + self.model.download_bytes


def _https_source(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("capability source is invalid")
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("capability source is invalid")
    return normalized


def load_mem0_capability_bom(
    manifest_path: Path,
    requirements_path: Path,
) -> Mem0CapabilityBOM:
    try:
        payload: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        requirements_bytes = requirements_path.read_bytes()
        requirements_digest = hashlib.sha256(requirements_bytes).hexdigest()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("capability BOM is unavailable") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "capability",
        "status",
        "version",
        "runtime",
        "model",
        "license_summary",
        "requires_gpu",
    }:
        raise ValueError("capability BOM is invalid")
    runtime = payload.get("runtime")
    model = payload.get("model")
    if (
        payload.get("schema_version") != "olivia.capability-bom.v1"
        or payload.get("capability") != "long_term_memory"
        or payload.get("status") != "FIXED"
        or not isinstance(payload.get("version"), str)
        or not isinstance(runtime, dict)
        or not isinstance(model, dict)
        or not isinstance(payload.get("license_summary"), str)
        or type(payload.get("requires_gpu")) is not bool
    ):
        raise ValueError("capability BOM is invalid")
    if set(runtime) != {
        "artifact_id",
        "canonical_source",
        "requirements",
        "artifacts",
        "requirements_sha256",
        "package_count",
        "download_size_bytes",
        "license",
        "redistributable",
        "required",
        "target",
        "install_mode",
        "status",
        "pypi_sources",
    }:
        raise ValueError("capability runtime BOM is invalid")
    runtime_sources = runtime.get("pypi_sources")
    if (
        runtime.get("artifact_id") != "mem0-runtime-windows-cp312"
        or runtime.get("canonical_source") != "https://pypi.org/simple"
        or
        runtime.get("requirements") != "installer/mem0-runtime-requirements.txt"
        or runtime.get("artifacts") != "installer/mem0-runtime-artifacts.json"
        or runtime.get("requirements_sha256") != requirements_digest
        or not _SHA256_RE.fullmatch(str(runtime.get("requirements_sha256", "")))
        or type(runtime.get("package_count")) is not int
        or runtime["package_count"] <= 0
        or type(runtime.get("download_size_bytes")) is not int
        or runtime["download_size_bytes"] <= 0
        or not isinstance(runtime.get("license"), str)
        or runtime.get("redistributable") is not False
        or runtime.get("required") is not False
        or runtime.get("target") != "runtime/mem0-site-packages"
        or runtime.get("install_mode") != "on_demand"
        or runtime.get("status") != "FIXED"
        or not isinstance(runtime_sources, dict)
        or set(runtime_sources) != {"mirror", "official"}
    ):
        raise ValueError("capability runtime BOM is invalid")
    if set(model) != {
        "artifact_id",
        "canonical_source",
        "repo_id",
        "revision",
        "license",
        "redistributable",
        "required",
        "target",
        "install_mode",
        "status",
        "sources",
        "files",
    }:
        raise ValueError("capability model BOM is invalid")
    model_sources = model.get("sources")
    raw_files = model.get("files")
    if (
        model.get("artifact_id") != "BAAI/bge-small-zh-v1.5"
        or model.get("canonical_source")
        != "https://huggingface.co/BAAI/bge-small-zh-v1.5"
        or model.get("repo_id") != "BAAI/bge-small-zh-v1.5"
        or not isinstance(model.get("revision"), str)
        or not _REVISION_RE.fullmatch(model["revision"])
        or not isinstance(model.get("license"), str)
        or model.get("redistributable") is not True
        or model.get("required") is not False
        or model.get("target") != "data/memory/model-cache"
        or model.get("install_mode") != "on_demand"
        or model.get("status") != "FIXED"
        or not isinstance(model_sources, dict)
        or set(model_sources) != {"mirror", "official"}
        or not isinstance(raw_files, dict)
        or len(raw_files) != 10
    ):
        raise ValueError("capability model BOM is invalid")
    files: dict[str, ModelArtifact] = {}
    for name, item in raw_files.items():
        if (
            not isinstance(name, str)
            or not name
            or "\\" in name
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or not isinstance(item, dict)
            or set(item) != {"size_bytes", "sha256"}
            or type(item.get("size_bytes")) is not int
            or not isinstance(item.get("sha256"), str)
        ):
            raise ValueError("capability model file is invalid")
        files[name] = ModelArtifact(item["size_bytes"], item["sha256"])
    try:
        artifact_payload: Any = json.loads(
            manifest_path.with_name("mem0-runtime-artifacts.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("capability runtime artifacts are unavailable") from exc
    raw_artifacts = artifact_payload.get("artifacts") if isinstance(artifact_payload, dict) else None
    if (
        not isinstance(artifact_payload, dict)
        or set(artifact_payload) != {"schema_version", "artifacts"}
        or artifact_payload.get("schema_version")
        != "olivia.capability-runtime-artifacts.v1"
        or not isinstance(raw_artifacts, list)
        or len(raw_artifacts) != runtime["package_count"]
    ):
        raise ValueError("capability runtime artifacts are invalid")
    artifacts: list[RuntimeArtifact] = []
    names: set[str] = set()
    for item in raw_artifacts:
        if not isinstance(item, dict) or set(item) != {
            "filename",
            "size_bytes",
            "sha256",
            "license",
        }:
            raise ValueError("capability runtime artifact is invalid")
        filename = item.get("filename")
        digest = item.get("sha256")
        if (
            not isinstance(filename, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.whl", filename)
            or filename.casefold() in names
            or type(item.get("size_bytes")) is not int
            or item["size_bytes"] <= 0
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
            or not isinstance(item.get("license"), str)
            or not item["license"]
        ):
            raise ValueError("capability runtime artifact is invalid")
        names.add(filename.casefold())
        artifacts.append(
            RuntimeArtifact(filename, item["size_bytes"], digest, item["license"])
        )
    requirements_hashes = set(
        re.findall(rb"--hash=sha256:([0-9a-f]{64})", requirements_bytes)
    )
    if (
        {item.sha256.encode("ascii") for item in artifacts} != requirements_hashes
        or sum(item.size_bytes for item in artifacts) != runtime["download_size_bytes"]
    ):
        raise ValueError("capability runtime artifact closure is invalid")
    return Mem0CapabilityBOM(
        capability=payload["capability"],
        status=payload["status"],
        version=payload["version"],
        runtime=RuntimeBOM(
            requirements_sha256=requirements_digest,
            package_count=runtime["package_count"],
            estimated_download_bytes=runtime["download_size_bytes"],
            sources=(
                _https_source(runtime_sources["mirror"]),
                _https_source(runtime_sources["official"]),
            ),
            artifacts=tuple(artifacts),
        ),
        model=ModelBOM(
            repo_id=model["repo_id"],
            revision=model["revision"],
            license=model["license"],
            sources=(
                _https_source(model_sources["mirror"]),
                _https_source(model_sources["official"]),
            ),
            files=files,
        ),
        license_summary=payload["license_summary"],
        requires_gpu=payload["requires_gpu"],
    )


class ResumableModelDownloader:
    """Download immutable model files with Range resume and source fallback."""

    def __init__(
        self,
        *,
        repo_id: str,
        revision: str,
        files: Mapping[str, ModelArtifact],
        sources: tuple[str, str],
        download_root: Path,
        source_mode: str,
        pause_requested: threading.Event,
        progress: Progress,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if source_mode not in {"auto", "official"}:
            raise ValueError("download source mode is invalid")
        self.repo_id = repo_id
        self.revision = revision
        self.files = files
        self.sources = sources[1:] if source_mode == "official" else sources
        self.download_root = download_root
        self.pause_requested = pause_requested
        self.progress = progress
        self.opener = opener
        self.total_bytes = sum(item.size_bytes for item in files.values())
        self.completed_bytes = 0
        self._completed: set[str] = set()
        self.last_source: str | None = None
        self._source_history: list[str] = []

    def _record_source(self, source: str) -> None:
        if source not in self._source_history:
            self._source_history.append(source)
        self.last_source = ";".join(self._source_history)

    def download(
        self,
        *,
        revision: str,
        relative_path: str,
        destination: Path,
    ) -> None:
        if revision != self.revision or relative_path not in self.files:
            raise RuntimeError("MEM0_EMBEDDING_IDENTITY_MISMATCH")
        artifact = self.files[relative_path]
        cached = self.download_root.joinpath(*relative_path.split("/"))
        cached_source = cached.with_name(cached.name + ".source")
        partial = cached.with_suffix(cached.suffix + ".part")
        partial_source = partial.with_name(partial.name + ".source")
        cached.parent.mkdir(parents=True, exist_ok=True)
        if not self._valid(cached, artifact):
            cached.unlink(missing_ok=True)
            if partial.is_file() and partial.stat().st_size > artifact.size_bytes:
                partial.unlink()
                partial_source.unlink(missing_ok=True)
            last_error: Exception | None = None
            for source in self.sources:
                try:
                    try:
                        recorded_source = partial_source.read_text(encoding="utf-8")
                    except OSError:
                        recorded_source = ""
                    if partial.is_file() and recorded_source != source:
                        partial.unlink()
                        partial_source.unlink(missing_ok=True)
                    partial_source.write_text(source, encoding="utf-8")
                    self.last_source = source
                    self._transfer(source, relative_path, partial, artifact)
                    if not self._valid(partial, artifact):
                        partial.unlink(missing_ok=True)
                        partial_source.unlink(missing_ok=True)
                        raise RuntimeError("MEM0_MODEL_HASH_MISMATCH")
                    last_error = None
                    break
                except _DownloadPaused:
                    raise
                except (OSError, RuntimeError) as exc:
                    last_error = exc
            if last_error is not None:
                raise RuntimeError("MEM0_MODEL_DOWNLOAD_FAILED") from last_error
            partial.replace(cached)
            partial_source.unlink(missing_ok=True)
            cached_source.write_text(str(self.last_source), encoding="utf-8")
            self._record_source(str(self.last_source))
        else:
            try:
                source = cached_source.read_text(encoding="utf-8")
            except OSError:
                source = "verified-legacy-download-cache"
            self._record_source(
                source if source in self.sources else "verified-legacy-download-cache"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cached, destination)
        if relative_path not in self._completed:
            self.completed_bytes += artifact.size_bytes
            self._completed.add(relative_path)
        self.progress(self.completed_bytes, self.total_bytes, relative_path)

    def _transfer(
        self,
        source: str,
        relative_path: str,
        partial: Path,
        artifact: ModelArtifact,
    ) -> None:
        offset = partial.stat().st_size if partial.is_file() else 0
        url = (
            f"{source}/{self.repo_id}/resolve/{self.revision}/"
            f"{quote(relative_path, safe='/')}"
        )
        request = Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
        with self.opener(request, timeout=30) as response:
            status = getattr(response, "status", 200)
            append = bool(offset and status == 206)
            if offset and not append:
                offset = 0
            mode = "ab" if append else "wb"
            with partial.open(mode) as stream:
                downloaded = offset
                while True:
                    if self.pause_requested.is_set():
                        raise _DownloadPaused
                    block = response.read(1 << 20)
                    if not block:
                        break
                    stream.write(block)
                    downloaded += len(block)
                    self.progress(
                        self.completed_bytes + downloaded,
                        self.total_bytes,
                        relative_path,
                    )
        if partial.stat().st_size != artifact.size_bytes:
            raise OSError("download size mismatch")

    @staticmethod
    def _valid(path: Path, artifact: ModelArtifact) -> bool:
        try:
            if path.stat().st_size != artifact.size_bytes:
                return False
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest() == artifact.sha256
        except OSError:
            return False


class _DownloadPaused(RuntimeError):
    pass


class CapabilityLayer(Protocol):
    def ready(self) -> bool: ...

    def install(
        self,
        *,
        source_mode: str,
        offline_root: Path | None,
        pause_requested: threading.Event,
        progress: Progress,
    ) -> None: ...

    def uninstall(self) -> None: ...


CommandRunner = Callable[..., int]
RuntimeVerifier = Callable[[Path, Path], bool]


def _run_command(
    command: list[str],
    *,
    environment: Mapping[str, str],
    pause_requested: threading.Event,
) -> int:
    process = subprocess.Popen(
        command,
        env=dict(environment),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    while process.poll() is None:
        if pause_requested.is_set():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise _DownloadPaused
        time.sleep(0.1)
    return int(process.returncode or 0)


class ManagedMem0Runtime:
    """Install the hash-locked Mem0 wheel closure beside the core runtime."""

    def __init__(
        self,
        *,
        install_root: Path,
        python_executable: Path,
        requirements: Path,
        sources: tuple[str, str],
        download_bytes: int,
        verifier: RuntimeVerifier | None = None,
        runner: CommandRunner = _run_command,
    ) -> None:
        self.owner_root = install_root.absolute()
        self.install_root = install_root.resolve()
        self.python_executable = python_executable.resolve()
        self.requirements = requirements.resolve()
        self.sources = sources
        self.download_bytes = download_bytes
        self.verifier = verifier
        self.runner = runner
        self.target = self.install_root / "runtime" / "mem0-site-packages"
        self.staging = self.install_root / "runtime" / "mem0-site-packages.staging"
        self.cache = self.install_root / "downloads" / "pip-cache"
        self._ready_fingerprint: tuple[int, ...] | None = None
        self._ready_result = False
        self.last_source: str | None = None
        try:
            self.python_executable.relative_to(self.install_root / "runtime")
            self.requirements.relative_to(self.install_root / "local_backend")
        except ValueError as exc:
            raise ValueError("managed Mem0 paths are invalid") from exc

    def _pth(self) -> Path:
        candidates = tuple(self.python_executable.parent.glob("*._pth"))
        if len(candidates) != 1 or not candidates[0].is_file():
            raise RuntimeError("MEM0_RUNTIME_PTH_UNAVAILABLE")
        return candidates[0]

    def _registered(self) -> bool:
        try:
            lines = self._pth().read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError, RuntimeError):
            return False
        expected = [str(self.target), str(self.target / "win32"), str(self.target / "win32" / "lib")]
        return lines[:3] == expected

    def ready(self) -> bool:
        try:
            if not self._registered():
                return False
            marker = json.loads(
                (self.target / ".olivia-mem0-runtime-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            if (
                marker.get("requirements_sha256")
                != hashlib.sha256(self.requirements.read_bytes()).hexdigest()
                or marker.get("source") not in self.sources
            ):
                return False
            self.last_source = marker["source"]
            tracked = (
                self.target,
                self.target / ".olivia-mem0-runtime-manifest.json",
                self._pth(),
                self.requirements,
            )
            fingerprint = tuple(
                value
                for path in tracked
                for value in (path.stat().st_mtime_ns, path.stat().st_size)
            )
            if fingerprint != self._ready_fingerprint:
                self._ready_result = self._verify(self.target)
                self._ready_fingerprint = fingerprint
            return self._ready_result
        except Exception:
            return False

    def _verify(self, runtime: Path) -> bool:
        if self.verifier is not None:
            return self.verifier(runtime, self.requirements)
        result = subprocess.run(
            [
                str(self.python_executable),
                str(self.requirements.parent / "verify_mem0_runtime.py"),
                str(runtime),
                str(self.requirements),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        return result.returncode == 0

    def _update_pth(self, *, enabled: bool) -> None:
        path = self._pth()
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        kept: list[str] = []
        has_site = False
        has_import = False
        for line in lines:
            stripped = line.strip()
            normalized = stripped.replace("\\", "/").casefold()
            if "/runtime/mem0-site-packages" in normalized:
                continue
            if stripped == "site-packages":
                if not has_site:
                    kept.append("site-packages")
                    has_site = True
                continue
            if stripped == "import site":
                if not has_import:
                    kept.append("import site")
                    has_import = True
                continue
            kept.append(line)
        if not has_site:
            kept.append("site-packages")
        if not has_import:
            kept.append("import site")
        if enabled:
            kept[:0] = [
                str(self.target),
                str(self.target / "win32"),
                str(self.target / "win32" / "lib"),
            ]
        staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        staging.write_text("\n".join(kept) + "\n", encoding="utf-8")
        staging.replace(path)

    def _command(self, *, target: Path, source: str | None, wheelhouse: Path | None) -> list[str]:
        command = [
            str(self.python_executable),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-compile",
            "--target",
            str(target),
            "-r",
            str(self.requirements),
        ]
        if wheelhouse is not None:
            command[6:6] = ["--no-index", "--find-links", str(wheelhouse)]
        elif source is not None:
            command[6:6] = ["--index-url", source, "--cache-dir", str(self.cache)]
        return command

    def install(
        self,
        *,
        source_mode: str,
        offline_root: Path | None,
        pause_requested: threading.Event,
        progress: Progress,
    ) -> None:
        if self.ready():
            return
        attempts: tuple[tuple[str | None, Path | None], ...]
        if offline_root is not None:
            raise ValueError("offline capability packs are not enabled")
        if source_mode == "official":
            attempts = ((self.sources[1], None),)
        elif source_mode == "auto":
            attempts = ((self.sources[0], None), (self.sources[1], None))
        else:
            raise ValueError("runtime source mode is invalid")
        environment = dict(os.environ)
        environment.update(
            {
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
                "DO_NOT_TRACK": "1",
            }
        )
        installed = False
        for source, wheelhouse in attempts:
            shutil.rmtree(self.staging, ignore_errors=True)
            self.staging.mkdir(parents=True, exist_ok=True)
            marker = {
                "requirements_sha256": hashlib.sha256(
                    self.requirements.read_bytes()
                ).hexdigest(),
                "source": source,
            }
            (self.staging / ".olivia-mem0-runtime-manifest.json").write_text(
                json.dumps(marker, sort_keys=True), encoding="utf-8"
            )
            self.last_source = source
            progress(0, self.download_bytes, "python-dependencies")
            result = self.runner(
                self._command(target=self.staging, source=source, wheelhouse=wheelhouse),
                environment=environment,
                pause_requested=pause_requested,
            )
            if result == 0:
                self.last_source = "offline" if wheelhouse is not None else source
                installed = True
                break
        if not installed:
            shutil.rmtree(self.staging, ignore_errors=True)
            raise RuntimeError("MEM0_RUNTIME_DOWNLOAD_FAILED")
        if not self._verify(self.staging):
            shutil.rmtree(self.staging, ignore_errors=True)
            raise RuntimeError("MEM0_RUNTIME_VERIFY_FAILED")
        backup = self.target.with_name(f"{self.target.name}.backup.{uuid.uuid4().hex}")
        old_pth = self._pth().read_bytes()
        if self.target.exists():
            self.target.replace(backup)
        try:
            self.staging.replace(self.target)
            self._update_pth(enabled=True)
            if not self._verify(self.target):
                raise RuntimeError("MEM0_RUNTIME_VERIFY_FAILED")
        except Exception:
            shutil.rmtree(self.target, ignore_errors=True)
            if backup.exists():
                backup.replace(self.target)
            self._pth().write_bytes(old_pth)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        self._ready_fingerprint = None
        progress(self.download_bytes, self.download_bytes, "python-dependencies")

    def uninstall(self) -> None:
        marker_name = ".olivia-mem0-runtime-manifest.json"
        for path, relative in (
            (self.target, "runtime/mem0-site-packages"),
            (self.staging, "runtime/mem0-site-packages.staging"),
        ):
            safe_managed_target(self.owner_root, relative)
            if path.exists():
                marker = json.loads((path / marker_name).read_text(encoding="utf-8"))
                if marker.get("requirements_sha256") != hashlib.sha256(
                    self.requirements.read_bytes()
                ).hexdigest():
                    raise RuntimeError("MEM0_RUNTIME_OWNERSHIP_INVALID")
        self._update_pth(enabled=False)
        for path in (self.target, self.staging):
            if path.exists():
                shutil.rmtree(path)
            if path.exists():
                raise RuntimeError("MEM0_RUNTIME_UNINSTALL_FAILED")
        self._ready_fingerprint = None
        self._ready_result = False


class ManagedEmbeddingModel:
    """Install the trusted BGE revision through verified online transport."""

    def __init__(
        self,
        *,
        data_root: Path,
        install_root: Path,
        bom: ModelBOM,
        download_root: Path,
    ) -> None:
        from mem0_memory import Mem0Config

        self.bom = bom
        self.data_owner_root, self.install_owner_root = (
            data_root.absolute(), install_root.absolute()
        )
        self.data_root, self.install_root = data_root.resolve(), install_root.resolve()
        self.download_root = download_root
        self.config = Mem0Config(
            enabled=True,
            data_root=data_root / "memory" / "mem0",
            embedding_cache=data_root / "memory" / "model-cache",
        )
        self.last_source: str | None = None

    @property
    def _source_marker(self) -> Path:
        return self.config.model_cache / "olivia-mem0-capability-source.json"

    def _read_source(self) -> str | None:
        try:
            payload = json.loads(self._source_marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        accepted = {
            *self.bom.sources,
            "verified-existing-cache",
            "verified-download-cache",
            "verified-legacy-download-cache",
        }
        source = payload.get("source")
        if (
            set(payload) != {"repo_id", "revision", "source"}
            or payload.get("repo_id") != self.bom.repo_id
            or payload.get("revision") != self.bom.revision
            or not isinstance(source, str)
            or not source
            or any(value not in accepted for value in source.split(";"))
        ):
            return None
        return str(source)

    def _bom_cache_ready(self) -> bool:
        from mem0_memory import verified_embedding_cache

        if not verified_embedding_cache(self.config):
            return False
        manifest_path = self.config.model_cache / "olivia-mem0-embedding-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        expected = {name: artifact.sha256 for name, artifact in self.bom.files.items()}
        return manifest.get("files") == expected

    def _write_source(self, source: str) -> None:
        self._source_marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._source_marker.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "repo_id": self.bom.repo_id,
                    "revision": self.bom.revision,
                    "source": source,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self._source_marker)

    def ready(self) -> bool:
        if not self._bom_cache_ready():
            return False
        source = self._read_source()
        if source is None:
            return False
        self.last_source = source
        return True

    def migrate_verified_cache(self) -> bool:
        if self.ready():
            return True
        if not self._bom_cache_ready():
            return False
        self.last_source = "verified-existing-cache"
        self._write_source(self.last_source)
        return self.ready()

    def _ensure_download_ownership(self) -> None:
        self.install_root.mkdir(parents=True, exist_ok=True)
        safe_managed_target(
            self.install_owner_root,
            self.download_root.relative_to(self.install_root).as_posix(),
        )
        owner = self.download_root / ".olivia-mem0-downloads.json"
        expected_owner = {"repo_id": self.bom.repo_id, "revision": self.bom.revision}
        if owner.is_file():
            try:
                if json.loads(owner.read_text(encoding="utf-8")) != expected_owner:
                    raise RuntimeError("MEM0_MODEL_OWNERSHIP_INVALID")
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("MEM0_MODEL_OWNERSHIP_INVALID") from exc
            return
        allowed: set[str] = set()
        for relative_path in self.bom.files:
            allowed.update(
                {
                    relative_path,
                    relative_path + ".source",
                    relative_path + ".part",
                    relative_path + ".part.source",
                }
            )
        allowed_directories = {
            parent.as_posix()
            for name in allowed
            for parent in Path(name).parents
            if parent != Path(".")
        }
        if self.download_root.exists():
            for candidate in self.download_root.rglob("*"):
                relative = candidate.relative_to(self.download_root).as_posix()
                safe_managed_target(self.download_root, relative)
                if (
                    candidate.is_file()
                    and relative not in allowed
                    or candidate.is_dir()
                    and relative not in allowed_directories
                    or not candidate.is_file()
                    and not candidate.is_dir()
                ):
                    raise RuntimeError("MEM0_MODEL_OWNERSHIP_INVALID")
        self.download_root.mkdir(parents=True, exist_ok=True)
        temporary = owner.with_suffix(".tmp")
        temporary.write_text(json.dumps(expected_owner, sort_keys=True), encoding="utf-8")
        temporary.replace(owner)

    def install(
        self,
        *,
        source_mode: str,
        offline_root: Path | None,
        pause_requested: threading.Event,
        progress: Progress,
    ) -> None:
        from mem0_embedding_install import Mem0EmbeddingInstaller

        if self.ready():
            return
        if offline_root is not None:
            raise ValueError("offline capability packs are not enabled")
        cache_was_verified = self._bom_cache_ready()
        previous_source = self._read_source()
        self._ensure_download_ownership()
        downloader: ResumableModelDownloader

        def report(downloaded: int, total: int, current: str) -> None:
            self.last_source = downloader.last_source
            progress(downloaded, total, current)

        downloader = ResumableModelDownloader(
            repo_id=self.bom.repo_id,
            revision=self.bom.revision,
            files=self.bom.files,
            sources=self.bom.sources,
            download_root=self.download_root,
            source_mode=source_mode,
            pause_requested=pause_requested,
            progress=report,
        )
        result = Mem0EmbeddingInstaller(
            self.config,
            downloader=downloader,
            expected_hashes={
                name: artifact.sha256 for name, artifact in self.bom.files.items()
            },
        ).install()
        if result.status not in {"APPLIED", "NOOP"} or not self._bom_cache_ready():
            if pause_requested.is_set():
                raise _DownloadPaused
            raise RuntimeError(result.reason_code or "MEM0_MODEL_INSTALL_FAILED")
        self.last_source = (
            downloader.last_source
            or previous_source
            or ("verified-existing-cache" if cache_was_verified else "verified-download-cache")
        )
        self._write_source(self.last_source)
        if not self.ready():
            raise RuntimeError("MEM0_MODEL_INSTALL_FAILED")

    def uninstall(self) -> None:
        snapshot = self.config.embedding_snapshot
        manifest = self.config.model_cache / "olivia-mem0-embedding-manifest.json"
        download_marker = self.download_root / ".olivia-mem0-downloads.json"
        if snapshot.exists() or manifest.exists() or self._source_marker.exists():
            safe_managed_target(
                self.data_owner_root, snapshot.relative_to(self.data_root).as_posix()
            )
        if self.download_root.exists():
            safe_managed_target(
                self.install_owner_root,
                self.download_root.relative_to(self.install_root).as_posix(),
            )
        expected = {name: item.sha256 for name, item in self.bom.files.items()}
        if snapshot.exists() and json.loads(manifest.read_text(encoding="utf-8")).get(
            "files"
        ) != expected:
            raise RuntimeError("MEM0_MODEL_OWNERSHIP_INVALID")
        if snapshot.exists() and self._read_source() is None:
            raise RuntimeError("MEM0_MODEL_OWNERSHIP_INVALID")
        if self.download_root.exists() and json.loads(
            download_marker.read_text(encoding="utf-8")
        ) != {"repo_id": self.bom.repo_id, "revision": self.bom.revision}:
            raise RuntimeError("MEM0_MODEL_OWNERSHIP_INVALID")
        if snapshot.exists():
            shutil.rmtree(snapshot)
        manifest.unlink(missing_ok=True)
        self._source_marker.unlink(missing_ok=True)
        if self.download_root.exists():
            shutil.rmtree(self.download_root)
        if snapshot.exists() or manifest.exists() or self.download_root.exists():
            raise RuntimeError("MEM0_MODEL_UNINSTALL_FAILED")


def create_mem0_capability_installer(
    *,
    install_root: Path,
    data_root: Path,
    python_executable: Path,
    backend_root: Path,
) -> Mem0CapabilityInstaller:
    manifest = backend_root / "installer" / "mem0-capability-manifest.json"
    requirements = backend_root / "installer" / "mem0-runtime-requirements.txt"
    bom = load_mem0_capability_bom(manifest, requirements)
    runtime = ManagedMem0Runtime(
        install_root=install_root,
        python_executable=python_executable,
        requirements=requirements,
        sources=bom.runtime.sources,
        download_bytes=bom.runtime.estimated_download_bytes,
    )
    model = ManagedEmbeddingModel(
        data_root=data_root,
        install_root=install_root,
        bom=bom.model,
        download_root=install_root / "downloads" / "mem0-model",
    )
    return Mem0CapabilityInstaller(
        runtime=runtime,
        model=model,
        version=bom.version,
        estimated_download_bytes=bom.estimated_download_bytes,
        license_summary=bom.license_summary,
        requires_gpu=bom.requires_gpu,
        runtime_download_bytes=bom.runtime.estimated_download_bytes,
        space_checks=(
            (
                lambda: shutil.disk_usage(install_root).free,
                max(1_073_741_824, bom.runtime.estimated_download_bytes * 2),
            ),
            (
                lambda: shutil.disk_usage(data_root).free,
                max(1_073_741_824, bom.model.download_bytes * 2),
            ),
        ),
    )


@dataclass(frozen=True)
class CapabilityStatus:
    state: CapabilityState
    phase: str
    downloaded_bytes: int
    total_bytes: int
    current_file: str | None
    source: str | None
    version: str
    license_summary: str
    requires_gpu: bool
    reason_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "olivia.capability-status.v1",
            "status": "READY" if self.state is CapabilityState.READY else "UNAVAILABLE",
            "capability": "long_term_memory",
            "state": self.state.value,
            "phase": self.phase,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "version": self.version,
            "license_summary": self.license_summary,
            "requires_gpu": self.requires_gpu,
        }
        if self.current_file is not None:
            payload["current_file"] = self.current_file
        if self.source is not None:
            payload["source"] = self.source
        if self.reason_code is not None:
            payload["reason_code"] = self.reason_code
        return payload


class Mem0CapabilityInstaller:
    """Coordinate runtime and model layers without downloading before consent."""

    def __init__(
        self,
        *,
        runtime: CapabilityLayer,
        model: CapabilityLayer,
        version: str,
        estimated_download_bytes: int,
        license_summary: str,
        requires_gpu: bool,
        runtime_download_bytes: int | None = None,
        required_free_bytes: int = 0,
        free_space: Callable[[], int] | None = None,
        space_checks: tuple[tuple[Callable[[], int], int], ...] = (),
    ) -> None:
        self.runtime = runtime
        self.model = model
        self.version = version
        self.total = estimated_download_bytes
        self.runtime_bytes = runtime_download_bytes or estimated_download_bytes // 2
        self.license_summary = license_summary
        self.requires_gpu = requires_gpu
        self.space_checks = space_checks or (
            ((free_space, max(0, required_free_bytes)),) if free_space else ()
        )
        self._lock = threading.Lock()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = self._new_status(CapabilityState.MISSING, "idle", 0)

    def _new_status(
        self,
        state: CapabilityState,
        phase: str,
        downloaded: int,
        *,
        current: str | None = None,
        source: str | None = None,
        reason: str | None = None,
    ) -> CapabilityStatus:
        return CapabilityStatus(
            state,
            phase,
            downloaded,
            self.total,
            current,
            source,
            self.version,
            self.license_summary,
            self.requires_gpu,
            reason,
        )

    def status(self) -> CapabilityStatus:
        try:
            ready = self.runtime.ready() and self.model.ready()
        except Exception:
            ready = False
        with self._lock:
            if ready and self._status.state in {
                CapabilityState.MISSING,
                CapabilityState.READY,
            }:
                self._status = self._new_status(
                    CapabilityState.READY,
                    "complete",
                    self.total,
                    source=self._actual_source(None),
                )
                return self._status
            if not ready and self._status.state is CapabilityState.READY:
                self._status = self._new_status(
                    CapabilityState.REPAIR,
                    "verification",
                    self._status.downloaded_bytes,
                    reason="MEM0_CAPABILITY_VERIFY_FAILED",
                )
            return self._status

    def _actual_source(self, fallback: str | None) -> str | None:
        values = [getattr(layer, "last_source", None) for layer in (self.runtime, self.model)]
        return ";".join(dict.fromkeys(value for value in values if value)) or fallback

    def _has_space(self) -> bool:
        try:
            return all(check() >= required for check, required in self.space_checks)
        except OSError:
            return False

    def _ready_after_migration(self) -> bool:
        if not self.runtime.ready():
            return False
        if self.model.ready():
            return True
        migrate = getattr(self.model, "migrate_verified_cache", None)
        return bool(callable(migrate) and migrate())

    def _progress(self, phase: str, layer: CapabilityLayer, source: str) -> Progress:
        def update(downloaded: int, total: int, current: str) -> None:
            ratio = 0 if total <= 0 else min(1.0, downloaded / total)
            base = 0 if phase == "runtime" else self.runtime_bytes
            span = self.runtime_bytes if phase == "runtime" else self.total - base
            with self._lock:
                self._status = self._new_status(
                    CapabilityState.DOWNLOADING,
                    phase,
                    min(self.total, base + int(span * ratio)),
                    current=Path(current).name[:160],
                    source=getattr(layer, "last_source", None) or source,
                )

        return update

    def install(
        self,
        *,
        source_mode: str,
        offline_root: Path | None = None,
    ) -> str:
        if source_mode not in {"auto", "official"} or offline_root is not None:
            raise ValueError("capability source mode is invalid")
        if self._ready_after_migration():
            with self._lock:
                self._status = self._new_status(
                    CapabilityState.READY,
                    "complete",
                    self.total,
                    source=self._actual_source(source_mode),
                )
            return "NOOP"
        if not self._has_space():
            with self._lock:
                self._status = self._new_status(
                    CapabilityState.REPAIR,
                    "preflight",
                    0,
                    source=source_mode,
                    reason="MEM0_CAPABILITY_DISK_SPACE_LOW",
                )
            return "REJECTED"
        with self._lock:
            if self._pause.is_set():
                self._status = self._new_status(
                    CapabilityState.PAUSED, "queued", 0, source=source_mode
                )
                return "PAUSED"
            self._status = self._new_status(
                CapabilityState.DOWNLOADING, "runtime", 0, source=source_mode
            )
        try:
            self.runtime.install(
                source_mode=source_mode,
                offline_root=offline_root,
                pause_requested=self._pause,
                progress=self._progress("runtime", self.runtime, source_mode),
            )
            if self._pause.is_set():
                raise _DownloadPaused
            self.model.install(
                source_mode=source_mode,
                offline_root=offline_root,
                pause_requested=self._pause,
                progress=self._progress("model", self.model, source_mode),
            )
            if self._pause.is_set():
                raise _DownloadPaused
            with self._lock:
                self._status = self._new_status(
                    CapabilityState.VERIFYING,
                    "verification",
                    self.total,
                    source=str(
                        self._actual_source(source_mode)
                    ),
                )
            if not self.runtime.ready() or not self.model.ready():
                raise RuntimeError("MEM0_CAPABILITY_VERIFY_FAILED")
        except _DownloadPaused:
            with self._lock:
                self._status = self._new_status(
                    CapabilityState.PAUSED,
                    self._status.phase,
                    self._status.downloaded_bytes,
                    current=self._status.current_file,
                    source=source_mode,
                )
            return "PAUSED"
        except Exception:
            with self._lock:
                self._status = self._new_status(
                    CapabilityState.REPAIR,
                    self._status.phase,
                    self._status.downloaded_bytes,
                    current=self._status.current_file,
                    source=source_mode,
                    reason="MEM0_CAPABILITY_INSTALL_FAILED",
                )
            return "REJECTED"
        with self._lock:
            self._status = self._new_status(
                CapabilityState.READY,
                "complete",
                self.total,
                source=self._actual_source(source_mode),
            )
        return "APPLIED"

    def start(
        self,
        *,
        source_mode: str,
        offline_root: Path | None = None,
    ) -> str:
        if source_mode not in {"auto", "official"} or offline_root is not None:
            raise ValueError("capability source mode is invalid")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return "NOOP"
            if self._ready_after_migration():
                return "NOOP"
            if not self._has_space():
                self._status = self._new_status(
                    CapabilityState.REPAIR,
                    "preflight",
                    0,
                    source=source_mode,
                    reason="MEM0_CAPABILITY_DISK_SPACE_LOW",
                )
                return "REJECTED"
            self._pause.clear()
            self._status = self._new_status(
                CapabilityState.QUEUED, "queued", 0, source=source_mode
            )
            self._thread = threading.Thread(
                target=self._install_background,
                kwargs={
                    "source_mode": source_mode,
                    "offline_root": offline_root,
                },
                name="olivia-mem0-capability-install",
                daemon=True,
            )
            self._thread.start()
        return "APPLIED"

    def _install_background(
        self,
        *,
        source_mode: str,
        offline_root: Path | None,
    ) -> None:
        self.install(source_mode=source_mode, offline_root=offline_root)

    def pause(self) -> str:
        with self._lock:
            if self._status.state not in {
                CapabilityState.QUEUED,
                CapabilityState.DOWNLOADING,
                CapabilityState.VERIFYING,
            }:
                return "NOOP"
            self._pause.set()
        return "APPLIED"

    def resume(self, *, source_mode: str) -> str:
        if self.status().state is CapabilityState.READY:
            return "NOOP"
        return self.start(source_mode=source_mode)

    def uninstall(self, *, remove_model: bool) -> str:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return "REJECTED"
        changed = self.runtime.ready() or remove_model and self.model.ready()
        try:
            self.runtime.uninstall()
            if remove_model:
                self.model.uninstall()
        except Exception:
            with self._lock:
                self._status = self._new_status(
                    CapabilityState.REPAIR,
                    "uninstall",
                    0,
                    reason="MEM0_CAPABILITY_UNINSTALL_FAILED",
                )
            return "REJECTED"
        with self._lock:
            self._status = self._new_status(CapabilityState.MISSING, "idle", 0)
        return "APPLIED" if changed else "NOOP"


__all__ = [
    "CapabilityState",
    "CapabilityStatus",
    "ManagedMem0Runtime",
    "ManagedEmbeddingModel",
    "ModelBOM",
    "Mem0CapabilityBOM",
    "Mem0CapabilityInstaller",
    "ModelArtifact",
    "ResumableModelDownloader",
    "create_mem0_capability_installer",
    "load_mem0_capability_bom",
]
