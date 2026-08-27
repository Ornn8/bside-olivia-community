from __future__ import annotations

import json
from pathlib import Path

import pytest

import local_memory
from installer import uninstall_safety
from installer.provision_mem0_embedding import provision_embedding
from installer.start_local import _configure_memory_environment
from installer.full_patch import copy_project_payload
from installer.uninstall_safety import OWNED_PATHS, remove_owned_targets
from local_memory import (
    NullConversationMemoryPort,
    create_conversation_memory_adapter,
    load_memory_config,
)


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
    assert environment["OLIVIA_MEMORY_DEFAULT_PROVIDER"] == "mem0"
    assert environment["OLIVIA_MEMORY_ROOT"] == str(tmp_path / "data" / "memory")
    assert environment["OLIVIA_MEMORY_EMBEDDING_CACHE"] == str(
        tmp_path / "data" / "memory" / "model-cache"
    )
    assert environment["OLIVIA_MEMORY_LLM_DEFAULT_BASE_URL"] == "https://gateway.example/v1"
    assert environment["OLIVIA_MEMORY_LLM_DEFAULT_MODEL"] == "fixture-model"
    assert environment["OLIVIA_MEMORY_LLM_DEFAULT_API_KEY_ENV"] == "FIXTURE_API_KEY"


def test_first_install_preserves_an_explicit_memory_opt_out(tmp_path: Path) -> None:
    environment = _configure_memory_environment(
        {"OLIVIA_MEMORY_ENABLED": "0"},
        tmp_path / "data",
    )

    assert environment["OLIVIA_MEMORY_ENABLED"] == "0"


@pytest.mark.parametrize("provider", ["sqlite", "none"])
def test_launcher_preserves_explicit_memory_provider_and_independent_endpoint(
    tmp_path: Path,
    provider: str,
) -> None:
    environment = _configure_memory_environment(
        {
            "OLIVIA_MEMORY_PROVIDER": provider,
            "OLIVIA_MEMORY_LLM_BASE_URL": "https://memory.example/v1",
            "OLIVIA_MEMORY_LLM_MODEL": "memory-only-model",
            "OLIVIA_MEMORY_LLM_API_KEY_ENV": "MEMORY_API_KEY",
            "OLIVIA_LLM_BASE_URL": "https://primary.example/v1",
        },
        tmp_path / "data",
    )

    assert environment["OLIVIA_MEMORY_PROVIDER"] == provider
    assert environment["OLIVIA_MEMORY_LLM_BASE_URL"] == "https://memory.example/v1"
    assert environment["OLIVIA_MEMORY_LLM_MODEL"] == "memory-only-model"
    assert environment["OLIVIA_MEMORY_LLM_API_KEY_ENV"] == "MEMORY_API_KEY"


def test_installed_launcher_preserves_file_provider_and_memory_llm_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "memory_config.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "provider": "mem0",
                "llm": {
                    "provider": "openai",
                    "base_url": "https://memory.example/v1",
                    "model": "memory-only-model",
                    "api_key_env": "MEMORY_API_KEY",
                },
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        local_memory,
        "create_mem0_adapter",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    environment = _configure_memory_environment(
        {
            "OLIVIA_LLM_BASE_URL": "https://primary.example/v1",
            "OLIVIA_LLM_MODEL": "primary-model",
            "OLIVIA_LLM_API_KEY_ENV": "PRIMARY_API_KEY",
        },
        tmp_path / "data",
    )
    config = load_memory_config(config_path, environ=environment, root=tmp_path)
    create_conversation_memory_adapter(config, environ=environment)

    assert config.provider == "mem0"
    assert config.llm["base_url"] == "https://memory.example/v1"
    assert captured["environ"]["OLIVIA_MEMORY_LLM_BASE_URL"] == "https://memory.example/v1"
    assert captured["environ"]["OLIVIA_MEMORY_LLM_MODEL"] == "memory-only-model"
    assert captured["environ"]["OLIVIA_MEMORY_LLM_API_KEY_ENV"] == "MEMORY_API_KEY"

    disabled = _configure_memory_environment(
        {"OLIVIA_MEMORY_PROVIDER": "none"}, tmp_path / "data"
    )
    disabled_config = load_memory_config(environ=disabled, root=tmp_path)
    assert disabled_config.config_error is None
    assert isinstance(
        create_conversation_memory_adapter(disabled_config, environ=disabled),
        NullConversationMemoryPort,
    )


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


def test_embeddable_python_registers_the_managed_memory_runtime_without_pythonpath() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")
    runtime_probe = script[
        script.index("function Test-MemoryRuntime") : script.index("$runner =")
    ]

    assert "[string]$MemoryRuntimePath" in script
    assert "-MemoryRuntimePath $memoryRuntime" in script
    assert "$env:PYTHONPATH" not in runtime_probe


def test_managed_memory_runtime_is_preferred_and_verified_against_its_lock_manifest() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")
    runtime_probe = script[
        script.index("function Test-MemoryRuntime") : script.index("$runner =")
    ]

    assert "$keptLines.Insert(0, $memoryRuntimeFullPath)" in script
    assert "mem0-runtime-manifest.json" in script
    assert "[string]$RequirementsPath" in runtime_probe
    assert "importlib.metadata" in runtime_probe
    assert "spec.origin" in runtime_probe
    assert "MEM0_RUNTIME_MANIFEST_INVALID" in runtime_probe


def test_installer_disables_mem0_telemetry_before_every_probe_import() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")
    launcher = (root / "installer" / "start_local.py").read_text(encoding="utf-8")

    telemetry_setting = "$env:MEM0_TELEMETRY = 'False'"
    assert telemetry_setting in script
    assert script.index(telemetry_setting) < script.index("import importlib.metadata")
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


def test_optional_memory_downgrades_do_not_leak_a_native_failure_exit_code() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")

    assert "$LASTEXITCODE = 0\n\n& (Join-Path $PSScriptRoot 'Create-Shortcut.ps1')" in script
    assert script.rstrip().endswith("exit 0")


def test_uninstall_removes_only_the_managed_memory_runtime(tmp_path: Path) -> None:
    managed_runtime = tmp_path / "runtime" / "mem0-site-packages"
    managed_runtime.mkdir(parents=True)
    (managed_runtime / "mem0.py").write_text("managed", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "letters.json").write_text("keep", encoding="utf-8")

    remove_owned_targets(tmp_path)

    assert not managed_runtime.exists()
    assert (tmp_path / "data" / "letters.json").read_text(encoding="utf-8") == "keep"


def test_uninstall_removes_only_its_exact_managed_python_path_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = tmp_path / "installation"
    managed_runtime = installation / "runtime" / "mem0-site-packages"
    managed_runtime.mkdir(parents=True)
    shared_runtime = tmp_path / "shared-runtime"
    shared_runtime.mkdir()
    executable = shared_runtime / "python.exe"
    executable.touch()
    pth = shared_runtime / "python312._pth"
    other_runtime = tmp_path / "other" / "runtime" / "mem0-site-packages"
    pth.write_text(
        f"{managed_runtime.resolve()}\n{other_runtime.resolve()}\nsite-packages\nimport site\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(uninstall_safety.sys, "executable", str(executable))

    remove_owned_targets(installation)

    registrations = pth.read_text(encoding="utf-8")
    assert str(managed_runtime.resolve()) not in registrations
    assert str(other_runtime.resolve()) in registrations


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
