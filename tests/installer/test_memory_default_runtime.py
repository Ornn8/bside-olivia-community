from __future__ import annotations

from pathlib import Path

from installer.start_local import _configure_memory_environment


def _base() -> dict[str, str]:
    return {
        "OLIVIA_LLM_BASE_URL": "https://api.example.invalid",
        "OLIVIA_LLM_MODEL": "fixture-model",
        "OLIVIA_LLM_API_KEY_ENV": "FIXTURE_API_KEY",
    }


def test_normal_start_enables_confined_offline_memory_by_default(
    tmp_path: Path,
) -> None:
    environment = _configure_memory_environment(_base(), tmp_path / "data")
    assert environment["OLIVIA_MEMORY_ENABLED"] == "1"
    assert environment["OLIVIA_MEMORY_INSTALLED_RUNTIME"] == "1"
    assert environment["OLIVIA_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory" / "mem0"
    )
    assert environment["OLIVIA_MEMORY_EMBEDDING_CACHE"] == str(
        tmp_path / "data" / "memory" / "model-cache"
    )
    assert environment["FASTEMBED_CACHE_PATH"] == environment[
        "OLIVIA_MEMORY_EMBEDDING_CACHE"
    ]
    assert environment["OLIVIA_MEMORY_EMBEDDING_MODEL"] == (
        "BAAI/bge-small-zh-v1.5"
    )
    assert environment["OLIVIA_MEMORY_EMBEDDING_DIMS"] == "512"
    assert environment["OLIVIA_MEMORY_LLM_BASE_URL"] == (
        "https://api.example.invalid"
    )
    assert environment["OLIVIA_MEMORY_LLM_MODEL"] == "fixture-model"
    assert environment["OLIVIA_MEMORY_LLM_API_KEY_ENV"] == (
        "FIXTURE_API_KEY"
    )
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert environment["DO_NOT_TRACK"] == "1"


def test_explicit_memory_disable_is_preserved(tmp_path: Path) -> None:
    environment = _base()
    environment["OLIVIA_MEMORY_ENABLED"] = "0"
    configured = _configure_memory_environment(environment, tmp_path / "data")
    assert configured["OLIVIA_MEMORY_ENABLED"] == "0"
    assert configured["OLIVIA_MEMORY_INSTALLED_RUNTIME"] == "1"


def test_windows_installer_keeps_memory_optional_and_verified() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "memory-runtime-requirements.txt" in script
    assert "--require-hashes" in script
    assert "--only-binary=:all:" in script
    assert "memory-model-manifest.json" in script
    assert "provision_memory_model.py" in script
    assert "--verify-only" in script
    assert "--provision" in script
    assert "Olivia will continue without long-term memory" in script
    assert "MEMORY_DEPENDENCIES_UNAVAILABLE" in script
    assert "MEMORY_MODEL_UNAVAILABLE" in script

    lock = (root / "installer" / "memory-runtime-requirements.txt").read_text(
        encoding="utf-8"
    )
    package_lines = [
        line
        for line in lock.splitlines()
        if line and not line.startswith("#") and not line.startswith("    ")
    ]
    assert package_lines
    assert all("==" in line and "--hash=sha256:" in line for line in package_lines)
    assert any(line.startswith("mem0ai==2.0.18") for line in package_lines)
    assert any(line.startswith("fastembed==0.8.0") for line in package_lines)
    assert "httpx2==" not in lock
    assert "httpcore2==" not in lock
