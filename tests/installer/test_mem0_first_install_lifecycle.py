from __future__ import annotations

from pathlib import Path

import pytest

from installer.provision_mem0_embedding import provision_embedding
from installer.start_local import _configure_memory_environment
from installer.full_patch import copy_project_payload
from installer.uninstall_safety import OWNED_PATHS, remove_owned_targets


def test_first_install_enables_mem0_in_install_owned_paths_by_default(
    tmp_path: Path,
) -> None:
    environment = _configure_memory_environment(
        {
            "OLIVIA_LLM_BASE_URL": "https://gateway.example/v1",
            "OLIVIA_LLM_MODEL": "fixture-model",
            "OLIVIA_LLM_API_KEY_ENV": "FIXTURE_API_KEY",
        },
        tmp_path / "data",
    )

    assert environment["OLIVIA_MEMORY_ENABLED"] == "1"
    assert environment["OLIVIA_MEMORY_PROVIDER"] == "mem0"
    assert environment["OLIVIA_MEMORY_ROOT"] == str(tmp_path / "data" / "memory")
    assert environment["OLIVIA_MEMORY_EMBEDDING_CACHE"] == str(
        tmp_path / "data" / "memory" / "model-cache"
    )
    assert environment["OLIVIA_MEMORY_LLM_BASE_URL"] == "https://gateway.example/v1"
    assert environment["OLIVIA_MEMORY_LLM_MODEL"] == "fixture-model"
    assert environment["OLIVIA_MEMORY_LLM_API_KEY_ENV"] == "FIXTURE_API_KEY"


def test_first_install_preserves_an_explicit_memory_opt_out(tmp_path: Path) -> None:
    environment = _configure_memory_environment(
        {"OLIVIA_MEMORY_ENABLED": "0"},
        tmp_path / "data",
    )

    assert environment["OLIVIA_MEMORY_ENABLED"] == "0"


def test_windows_installer_offers_pinned_mem0_and_confirmed_embedding_setup() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")
    requirements = (
        root / "installer" / "mem0-runtime-requirements.txt"
    ).read_text(encoding="utf-8")

    assert "mem0-runtime-requirements.txt" in script
    assert "--only-binary=:all:" in script
    assert "MEMORY_DEPENDENCIES_UNAVAILABLE" in script
    assert "MEMORY_DEPENDENCIES_NOT_ACCEPTED" in script
    assert "provision_mem0_embedding.py" in script
    assert "--verify-only" in script
    assert "--install" in script
    assert "MEMORY_EMBEDDING_UNAVAILABLE" in script
    assert "MEMORY_EMBEDDING_NOT_ACCEPTED" in script
    assert "Olivia will continue without long-term memory" in script
    assert "mem0ai==2.0.18" in requirements
    assert "sentence-transformers==5.7.0" in requirements


def test_optional_memory_runtime_is_a_hash_locked_install_owned_closure() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")
    requirements = (
        root / "installer" / "mem0-runtime-requirements.txt"
    ).read_text(encoding="utf-8")

    assert "runtime\\mem0-site-packages" in script
    assert "mem0-site-packages.staging" in script
    assert "[IO.Directory]::Move" in script
    assert "'--require-hashes'" in script
    assert "'--upgrade'" not in script
    assert "runtime/mem0-site-packages" in OWNED_PATHS
    package_lines = [
        line
        for line in requirements.splitlines()
        if line and not line.startswith("#")
    ]
    assert package_lines
    assert all("==" in line and " --hash=sha256:" in line for line in package_lines)


def test_installer_disables_mem0_telemetry_before_every_probe_import() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")
    launcher = (root / "installer" / "start_local.py").read_text(encoding="utf-8")

    telemetry_setting = "$env:MEM0_TELEMETRY = 'False'"
    assert telemetry_setting in script
    assert script.index(telemetry_setting) < script.index("import mem0")
    assert '"MEM0_TELEMETRY": "False"' in launcher


def test_memory_download_confirmations_show_complete_informed_choices() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")

    assert "Components (complete package/version/SHA-256 closure):" in script
    assert "$memoryRequirementLines | Write-Host" in script
    assert "Estimated download: about 225 MiB" in script
    assert "Source: PyPI, exact versions and SHA-256 hashes above" in script
    assert "Licenses: mem0ai 2.0.18 Apache-2.0" in script
    assert "BAAI/bge-small-zh-v1.5 at revision 7999e1d3359715c523056ef9478215996d62a620" in script
    assert "Contents: 10 pinned files:" in script
    assert "model.safetensors" in script
    assert "Estimated download: about 96 MiB" in script
    assert "License: MIT" in script


def test_uninstall_removes_only_the_managed_memory_runtime(tmp_path: Path) -> None:
    managed_runtime = tmp_path / "runtime" / "mem0-site-packages"
    managed_runtime.mkdir(parents=True)
    (managed_runtime / "mem0.py").write_text("managed", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "letters.json").write_text("keep", encoding="utf-8")

    remove_owned_targets(tmp_path)

    assert not managed_runtime.exists()
    assert (tmp_path / "data" / "letters.json").read_text(encoding="utf-8") == "keep"


def test_embedding_provision_entry_rejects_relative_install_paths() -> None:
    def must_not_construct(_config: object) -> object:
        raise AssertionError("relative paths must be rejected before installation")

    with pytest.raises(ValueError, match="absolute"):
        provision_embedding(
            memory_root=Path("memory"),
            embedding_cache=Path("model-cache"),
            installer_factory=must_not_construct,  # type: ignore[arg-type]
        )


def test_installed_payload_includes_the_embedding_provision_entry(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    destination = tmp_path / "local_backend"

    copy_project_payload(root, destination)

    assert (destination / "installer" / "provision_mem0_embedding.py").is_file()
    assert (destination / "installer" / "mem0-runtime-requirements.txt").is_file()
