from __future__ import annotations

from pathlib import Path

import pytest

from installer.provision_mem0_embedding import provision_embedding
from installer.start_local import _configure_memory_environment
from installer.full_patch import copy_project_payload


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
