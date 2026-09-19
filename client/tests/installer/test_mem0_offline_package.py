from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from mem0_capability_install import (
    CapabilityState,
    Mem0CapabilityBOM,
    Mem0CapabilityInstaller,
    Mem0OfflinePackage,
    ModelArtifact,
    ModelBOM,
    RuntimeArtifact,
    RuntimeBOM,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Mem0OfflinePackage, bytes, bytes]:
    wheel = b"synthetic wheel"
    model = b"synthetic model"
    capability = tmp_path / "mem0-capability-manifest.json"
    artifacts = tmp_path / "mem0-runtime-artifacts.json"
    requirements = tmp_path / "mem0-runtime-requirements.txt"
    capability.write_bytes(b'{"trusted":"capability"}')
    artifacts.write_bytes(b'{"trusted":"artifacts"}')
    requirements.write_bytes(b"trusted requirements")
    bom = Mem0CapabilityBOM(
        capability="long_term_memory",
        status="FIXED",
        version="fixture-v1",
        runtime=RuntimeBOM(
            requirements_sha256=_sha256(requirements.read_bytes()),
            package_count=1,
            estimated_download_bytes=len(wheel),
            sources=("https://mirror.example", "https://official.example"),
            artifacts=(
                RuntimeArtifact(
                    "fixture-1.0-py3-none-any.whl",
                    len(wheel),
                    _sha256(wheel),
                    "MIT",
                ),
            ),
        ),
        model=ModelBOM(
            repo_id="owner/model",
            revision="a" * 40,
            license="MIT",
            sources=("https://mirror.example", "https://official.example"),
            source_revisions=("b" * 40, "a" * 40),
            files={"model.bin": ModelArtifact(len(model), _sha256(model))},
        ),
        license_summary="fixture",
        requires_gpu=False,
    )
    package_manifest = {
        "schema_version": "olivia.memory-offline-private.v1",
        "capability": "long_term_memory",
        "version": bom.version,
        "capability_manifest_sha256": _sha256(capability.read_bytes()),
        "requirements_sha256": _sha256(requirements.read_bytes()),
        "runtime_artifacts_sha256": _sha256(artifacts.read_bytes()),
        "wheel_count": 1,
        "model": {
            "repo_id": bom.model.repo_id,
            "revision": bom.model.revision,
            "files": {
                "model.bin": {
                    "size_bytes": len(model),
                    "sha256": _sha256(model),
                }
            },
        },
    }
    archive = tmp_path / "Olivia-memory-offline-private.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as payload:
        payload.writestr(
            "olivia-memory-offline-manifest.json",
            json.dumps(package_manifest, sort_keys=True),
        )
        payload.write(capability, "installer/mem0-capability-manifest.json")
        payload.write(artifacts, "installer/mem0-runtime-artifacts.json")
        payload.write(requirements, "installer/mem0-runtime-requirements.txt")
        payload.writestr("wheelhouse/fixture-1.0-py3-none-any.whl", wheel)
        payload.writestr("model/model.bin", model)
    package = Mem0OfflinePackage(
        archive=archive,
        staging_parent=tmp_path / "staging",
        bom=bom,
        capability_manifest=capability,
        runtime_artifacts=artifacts,
        requirements=requirements,
    )
    return archive, package, wheel, model


def _replace_member(archive: Path, name: str, replacement: bytes) -> None:
    rewritten = archive.with_suffix(".rewritten.zip")
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(
        rewritten, "w", compression=zipfile.ZIP_STORED
    ) as target:
        for item in source.infolist():
            target.writestr(item.filename, replacement if item.filename == name else source.read(item))
    rewritten.replace(archive)


def test_memory_offline_package_releases_only_verified_payloads_to_temporary_staging(
    tmp_path: Path,
) -> None:
    _archive, package, wheel, model = _fixture(tmp_path)

    with package.prepare() as staging:
        assert (staging / "wheelhouse" / "fixture-1.0-py3-none-any.whl").read_bytes() == wheel
        assert (staging / "model" / "model.bin").read_bytes() == model
        assert sorted(path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()) == [
            "model/model.bin",
            "wheelhouse/fixture-1.0-py3-none-any.whl",
        ]

    assert not staging.exists()


def test_memory_offline_package_rejects_unlisted_traversal_members(
    tmp_path: Path,
) -> None:
    archive, package, _wheel, _model = _fixture(tmp_path)
    with zipfile.ZipFile(archive, "a") as payload:
        payload.writestr("../run-me.ps1", "Write-Output unsafe")

    with pytest.raises(RuntimeError, match="MEM0_OFFLINE_PACKAGE_INVALID"):
        with package.prepare():
            pytest.fail("an unsafe package must not expose a staging directory")

    assert not package.staging_parent.exists()


def test_memory_offline_package_rejects_same_size_payload_with_wrong_sha256(
    tmp_path: Path,
) -> None:
    archive, package, wheel, _model = _fixture(tmp_path)
    replacement = b"tampered wheel!"
    assert len(replacement) == len(wheel)
    _replace_member(
        archive,
        "wheelhouse/fixture-1.0-py3-none-any.whl",
        replacement,
    )

    with pytest.raises(RuntimeError, match="MEM0_OFFLINE_PACKAGE_HASH_MISMATCH"):
        with package.prepare():
            pytest.fail("a hash-mismatched package must not be installable")


def test_memory_capability_installs_both_layers_from_one_verified_offline_staging(
    tmp_path: Path,
) -> None:
    archive, package, wheel, model = _fixture(tmp_path)

    class Layer:
        def __init__(self, member: str, expected: bytes) -> None:
            self.member = member
            self.expected = expected
            self.is_ready = False
            self.sources: list[str] = []

        def ready(self) -> bool:
            return self.is_ready

        def install(self, *, source_mode, offline_root, pause_requested, progress) -> None:
            assert source_mode == "offline"
            assert offline_root is not None
            assert not pause_requested.is_set()
            assert (offline_root / self.member).read_bytes() == self.expected
            self.sources.append(source_mode)
            self.is_ready = True
            progress(len(self.expected), len(self.expected), self.member)

        def uninstall(self) -> None:
            self.is_ready = False

    runtime = Layer("wheelhouse/fixture-1.0-py3-none-any.whl", wheel)
    embedding = Layer("model/model.bin", model)
    selected: list[Path] = []

    def package_factory(path: Path) -> Mem0OfflinePackage:
        selected.append(path)
        return package

    installer = Mem0CapabilityInstaller(
        runtime=runtime,
        model=embedding,
        version="fixture-v1",
        estimated_download_bytes=len(wheel) + len(model),
        runtime_download_bytes=len(wheel),
        license_summary="fixture",
        requires_gpu=False,
        offline_package_factory=package_factory,
    )

    assert installer.install(source_mode="offline", offline_root=archive) == "APPLIED"
    assert selected == [archive]
    assert runtime.sources == ["offline"]
    assert embedding.sources == ["offline"]
    assert installer.status().state is CapabilityState.READY
    assert package.staging_parent.is_dir()
    assert not any(package.staging_parent.iterdir())


def test_invalid_memory_offline_package_is_rejected_before_either_layer_runs(
    tmp_path: Path,
) -> None:
    archive, package, wheel, model = _fixture(tmp_path)
    _replace_member(
        archive,
        "model/model.bin",
        b"x" * len(model),
    )

    class Layer:
        def ready(self) -> bool:
            return False

        def install(self, **_kwargs) -> None:
            pytest.fail("invalid packages must be rejected before layer installation")

        def uninstall(self) -> None:
            pass

    installer = Mem0CapabilityInstaller(
        runtime=Layer(),
        model=Layer(),
        version="fixture-v1",
        estimated_download_bytes=len(wheel) + len(model),
        license_summary="fixture",
        requires_gpu=False,
        offline_package_factory=lambda _path: package,
    )

    assert installer.install(source_mode="offline", offline_root=archive) == "REJECTED"
    status = installer.status()
    assert status.state is CapabilityState.REPAIR
    assert status.reason_code == "MEM0_CAPABILITY_INSTALL_FAILED"
