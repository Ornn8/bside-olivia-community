from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import subprocess
import sys

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


def test_memory_assets_remain_pinned_but_are_not_installed_during_first_setup() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")
    requirements = (
        root / "installer" / "mem0-runtime-requirements.txt"
    ).read_text(encoding="utf-8")

    assert "mem0-runtime-requirements.txt" not in script
    assert "provision_mem0_embedding.py" not in script
    assert "mem0ai==2.0.18" in requirements
    assert "sentence-transformers==5.7.0" in requirements
    assert (root / "installer" / "provision_mem0_embedding.py").is_file()


def test_memory_runtime_probe_accepts_a_hash_locked_requirement_and_runtime(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    verifier = root / "installer" / "verify_mem0_runtime.py"

    runtime = tmp_path / "mem0-site-packages"
    runtime.mkdir()
    (runtime / "annotated_doc-0.0.5.dist-info").mkdir()
    (runtime / "annotated_doc-0.0.5.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: annotated-doc\nVersion: 0.0.5\n",
        encoding="utf-8",
    )
    for module in ("mem0", "sentence_transformers", "huggingface_hub"):
        package = runtime / module
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
    (runtime / "win32" / "lib").mkdir(parents=True)
    (runtime / "win32" / "lib" / "pywintypes.py").write_text("", encoding="utf-8")
    requirements = tmp_path / "mem0-runtime-requirements.txt"
    requirements.write_text(
        "annotated-doc==0.0.5 --hash=sha256:117bac03a25ede5df5440e855b32d556049ca169ead221505badf432fed4b101\n",
        encoding="utf-8",
    )
    (runtime / ".olivia-mem0-runtime-manifest.json").write_text(
        json.dumps(
            {"requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest()}
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(verifier), str(runtime), str(requirements)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_windows_installer_runtime_probe_survives_native_argument_quoting(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    verifier = root / "installer" / "verify_mem0_runtime.py"
    runtime = tmp_path / "mem0-site-packages"
    runtime.mkdir()
    (runtime / "annotated_doc-0.0.5.dist-info").mkdir()
    (runtime / "annotated_doc-0.0.5.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: annotated-doc\nVersion: 0.0.5\n",
        encoding="utf-8",
    )
    for module in ("mem0", "sentence_transformers", "huggingface_hub"):
        package = runtime / module
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
    (runtime / "win32" / "lib").mkdir(parents=True)
    (runtime / "win32" / "lib" / "pywintypes.py").write_text("", encoding="utf-8")
    requirements = tmp_path / "mem0-runtime-requirements.txt"
    requirements.write_text(
        "annotated-doc==0.0.5 --hash=sha256:fixture\n",
        encoding="utf-8",
    )
    (runtime / ".olivia-mem0-runtime-manifest.json").write_text(
        json.dumps(
            {"requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest()}
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(verifier), str(runtime), str(requirements)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_first_install_defers_memory_runtime_and_embedding_downloads() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(encoding="utf-8")
    launcher = (root / "installer" / "start_local.py").read_text(encoding="utf-8")

    assert "mem0-runtime-requirements.txt" not in script
    assert "provision_mem0_embedding.py" not in script
    assert "BAAI/bge-small-zh-v1.5" not in script
    assert "Read-Host 'Accept this optional" not in script
    assert "runtime/mem0-site-packages" in OWNED_PATHS
    assert (root / "installer" / "mem0-runtime-requirements.txt").is_file()
    assert (root / "installer" / "provision_mem0_embedding.py").is_file()
    assert '"MEM0_TELEMETRY": "False"' in launcher
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
