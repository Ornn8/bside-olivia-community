from __future__ import annotations

import json
import hashlib
from pathlib import Path
import zipfile

import pytest

from asr.cuda_toolchain import (
    CUDA_WINDOWS_BUILD_COMPONENTS,
    assemble_cuda_toolchain,
    build_command,
    build_environment,
    cuda_toolchain_status,
    inspect_cuda_transfer,
    load_manifest,
    select_cuda_packages,
    uninstall_cuda_toolchain,
)
from asr.errors import AsrError
from tools.asr_manage import main as asr_manage_main


def _write_manifest(path: Path, *, components: tuple[str, ...] = CUDA_WINDOWS_BUILD_COMPONENTS) -> None:
    manifest: dict[str, object] = {
        "release_label": "13.3.0",
        "release_product": "cuda",
    }
    for component in components:
        manifest[component] = {
            "name": component,
            "version": "13.3.33",
            "license": "CUDA Toolkit",
            "license_path": f"{component}/LICENSE.txt",
            "windows-x86_64": {
                "relative_path": f"{component}/windows-x86_64/{component}.zip",
                "sha256": "a" * 64,
                "size": 7,
            },
        }
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _write_transfer(tmp_path: Path, *, unsafe: bool = False) -> tuple[Path, Path]:
    transfer_root = tmp_path / "transfer"
    manifest_path = tmp_path / "redistrib.json"
    manifest: dict[str, object] = {
        "release_label": "13.3.0",
        "release_product": "cuda",
    }
    files_by_component = {
        "cccl": ["include/cccl/test.cuh"],
        "cuda_crt": ["include/crt/host_config.h"],
        "cuda_ctadvisor": ["bin/ctadvisor.exe"],
        "cuda_cudart": [
            "bin/x64/cudart64_13.dll",
            "include/cuda_runtime.h",
            "lib/x64/cudart.lib",
        ],
        "cuda_nvcc": ["bin/nvcc.exe", "bin/ptxas.exe"],
        "libcublas": [
            "include/cublas_v2.h",
            "lib/x64/cublas.lib",
            "lib/x64/cublasLt.lib",
        ],
        "libnvfatbin": ["lib/x64/nvfatbin.lib"],
        "libnvjitlink": ["lib/x64/nvJitLink.lib"],
        "libnvptxcompiler": ["lib/x64/nvptxcompiler_static.lib"],
        "libnvvm": ["nvvm/bin/cicc.exe"],
    }
    for component in CUDA_WINDOWS_BUILD_COMPONENTS:
        relative_path = f"{component}/windows-x86_64/{component}.zip"
        archive = transfer_root / relative_path
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w") as handle:
            if unsafe and component == "cccl":
                handle.writestr("root/../../outside.txt", b"unsafe")
            else:
                for relative in files_by_component[component]:
                    handle.writestr("root/" + relative, b"fixture")
                handle.writestr("root/LICENSE", b"license")
        raw = archive.read_bytes()
        manifest[component] = {
            "name": component,
            "version": "13.3.33",
            "license": "CUDA Toolkit",
            "license_path": f"{component}/LICENSE.txt",
            "windows-x86_64": {
                "relative_path": relative_path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            },
        }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, transfer_root


def test_manifest_selection_returns_the_pinned_windows_build_closure(tmp_path: Path) -> None:
    manifest_path = tmp_path / "redistrib.json"
    _write_manifest(manifest_path)

    manifest = load_manifest(manifest_path, strict=False)
    packages = select_cuda_packages(manifest)

    assert tuple(package.package for package in packages) == CUDA_WINDOWS_BUILD_COMPONENTS
    assert packages[0].relative_path == "cccl/windows-x86_64/cccl.zip"
    assert packages[-1].license_path == "libnvvm/LICENSE.txt"
    assert packages[0].size == 7


@pytest.mark.parametrize("drive", ("C:", "E:"))
def test_cuda_status_accepts_any_local_drive(drive: str) -> None:
    report = cuda_toolchain_status(Path(f"{drive}/bside/asr/cuda"))

    assert report["toolchain_root"] == str(Path(f"{drive}/bside/asr/cuda"))


@pytest.mark.parametrize(
    "value",
    (
        Path("C:/"),
        Path("C:/bside/../cuda"),
        Path("C:bside/cuda"),
        Path("relative/cuda"),
        Path("https://example.invalid/cuda"),
        Path("//server/share/cuda"),
    ),
)
def test_cuda_status_rejects_unsafe_roots(value: Path) -> None:
    with pytest.raises(AsrError, match="absolute local Windows path"):
        cuda_toolchain_status(value)


def test_strict_manifest_pin_rejects_size_or_hash_mismatch(tmp_path: Path) -> None:
    manifest_path = tmp_path / "redistrib.json"
    _write_manifest(manifest_path)

    with pytest.raises(AsrError) as caught:
        load_manifest(manifest_path)

    assert caught.value.code == "ASR_TOOLCHAIN_CORRUPT"


def test_transfer_inspection_reports_missing_archives(tmp_path: Path) -> None:
    manifest_path = tmp_path / "redistrib.json"
    _write_manifest(manifest_path)

    report = inspect_cuda_transfer(manifest_path, tmp_path / "transfer", strict=False)

    assert report["status"] == "missing"
    assert {item["code"] for item in report["packages"]} == {"ASR_TOOLCHAIN_MISSING"}


def test_transfer_inspection_reports_corrupt_archive_hash(tmp_path: Path) -> None:
    manifest_path = tmp_path / "redistrib.json"
    _write_manifest(manifest_path)
    archive = tmp_path / "transfer" / "cccl" / "windows-x86_64" / "cccl.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"wrong")

    report = inspect_cuda_transfer(manifest_path, tmp_path / "transfer", strict=False)

    cccl = next(item for item in report["packages"] if item["package"] == "cccl")
    assert report["status"] == "corrupt"
    assert cccl["code"] == "ASR_TOOLCHAIN_CORRUPT"


def test_transfer_inspection_rejects_zip_path_traversal(tmp_path: Path) -> None:
    manifest_path, transfer_root = _write_transfer(tmp_path, unsafe=True)

    report = inspect_cuda_transfer(manifest_path, transfer_root, strict=False)

    assert report["status"] == "invalid"
    cccl = next(item for item in report["packages"] if item["package"] == "cccl")
    assert cccl["code"] == "ASR_TOOLCHAIN_INVALID"


def test_assembly_dry_run_does_not_write_and_apply_is_idempotent(tmp_path: Path) -> None:
    manifest_path, transfer_root = _write_transfer(tmp_path)
    toolchain_root = tmp_path / "toolchain"

    dry_run = assemble_cuda_toolchain(manifest_path, transfer_root, toolchain_root, strict=False)
    assert dry_run["mode"] == "dry-run"
    assert dry_run["status"] == "ready"
    assert not toolchain_root.exists()

    applied = assemble_cuda_toolchain(manifest_path, transfer_root, toolchain_root, apply=True, strict=False)
    repeated = assemble_cuda_toolchain(manifest_path, transfer_root, toolchain_root, apply=True, strict=False)
    assert applied["status"]["status"] == "ready"
    assert applied["assembled"] is True
    assert repeated["idempotent"] is True
    assert (toolchain_root / "bin" / "nvcc.exe").is_file()


def test_build_contract_is_sm86_msvc_http_asr_only_and_uninstall_is_owned(tmp_path: Path) -> None:
    command = build_command(tmp_path / "source", tmp_path / "build")
    assert "-AsrOnly" in command
    assert "-Http" in command
    assert command[command.index("-CudaArch") + 1] == "86"
    assert command[command.index("-Compiler") + 1] == "msvc"

    manifest_path, transfer_root = _write_transfer(tmp_path)
    toolchain_root = tmp_path / "toolchain"
    assemble_cuda_toolchain(manifest_path, transfer_root, toolchain_root, apply=True, strict=False)
    environment = build_environment(toolchain_root, cuda_arch="86")
    assert environment["environment"]["CMAKE_CUDA_ARCHITECTURES"] == "86"
    assert environment["environment"]["CMAKE_CUDA_HOST_COMPILER"] == "cl"
    assert environment["native_acceptance"] is False

    dry_uninstall = uninstall_cuda_toolchain(toolchain_root)
    assert dry_uninstall["mode"] == "dry-run"
    assert toolchain_root.exists()
    removed = uninstall_cuda_toolchain(toolchain_root, apply=True)
    assert removed["deleted"] is True
    assert not toolchain_root.exists()


def test_management_cli_reports_unavailable_without_touching_the_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toolchain_root = tmp_path / "cuda"

    assert asr_manage_main(["cuda-toolchain", "--action", "status", "--cuda-root", str(toolchain_root)]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["toolchain"]["status"] == "missing"
    assert result["environment"]["native_acceptance"] is False
    assert not toolchain_root.exists()
