from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from installer import configure
import installer.start_local as start_local


def _installation(tmp_path: Path, *, with_entrypoint: bool = True) -> Path:
    root = tmp_path / "installed"
    backend = root / "local_backend"
    (backend / "installer").mkdir(parents=True)
    (backend / "local_server.py").write_text("# fixture", encoding="utf-8")
    if with_entrypoint:
        (backend / "original_client_server.py").write_text(
            "# fixture",
            encoding="utf-8",
        )
    (backend / "installer" / "full-patch-manifest.json").write_text(
        json.dumps({"client_version": "0.0.9.615"}),
        encoding="utf-8",
    )
    client = root / "app" / "0.0.9.615" / "Olivia.exe"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"fixture")
    return root


def test_launcher_starts_combined_server_before_original_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _installation(tmp_path)
    health = iter((False, True, True))
    commands: list[list[str]] = []
    client_commands: list[list[str]] = []

    class Process:
        @staticmethod
        def poll():
            return None

    def popen(command, **_kwargs):
        commands.append([str(value) for value in command])
        return Process()

    def call(command, **_kwargs):
        client_commands.append([str(value) for value in command])
        return 0

    monkeypatch.setattr(start_local, "_health", lambda _port: next(health))
    monkeypatch.setattr(
        start_local,
        "_backend_executable",
        lambda: Path("pythonw-fixture.exe"),
    )
    monkeypatch.setattr(start_local.subprocess, "Popen", popen)
    monkeypatch.setattr(start_local.subprocess, "call", call)

    result = start_local.main(["--install-root", str(root), "--port", "8899"])

    assert result == 0
    assert len(commands) == 1
    backend_command = commands[0]
    assert backend_command[:2] == ["pythonw-fixture.exe", "-c"]
    assert "sys.path.insert(0, backend)" in backend_command[2]
    assert backend_command[3:] == [
        str(root / "local_backend"),
        str(root / "local_backend" / "original_client_server.py"),
    ]
    assert client_commands[0][0].endswith("Olivia.exe")
    assert not backend_command[-1].endswith("local_server.py")


def test_launcher_loads_configured_dpapi_key_without_environment_or_key_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    if os.name != "nt":
        pytest.skip("DPAPI is only available on Windows")

    root = _installation(tmp_path)
    expected = "dpapi-launcher-regression-value"
    monkeypatch.setenv(
        "PSModulePath",
        str(
            Path(os.environ.get("WINDIR", r"C:\\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "Modules"
        ),
    )
    for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(configure.getpass, "getpass", lambda _prompt: expected)

    assert configure.main(["--installation", str(root)]) == 0
    protected_path = root / "data" / "config" / "deepseek_api_key.dpapi"
    assert protected_path.is_file()
    assert protected_path.read_text(encoding="utf-8").strip() != expected

    health = iter((False, True, True))
    backend_environments: list[dict[str, str]] = []
    client_environments: list[dict[str, str]] = []

    class Process:
        @staticmethod
        def poll():
            return None

    original_popen = start_local.subprocess.Popen

    def popen(_command, **kwargs):
        if "env" not in kwargs:
            return original_popen(_command, **kwargs)
        backend_environments.append(kwargs["env"].copy())
        return Process()

    def call(_command, **kwargs):
        client_environments.append(kwargs["env"].copy())
        return 0

    monkeypatch.setattr(start_local, "_health", lambda _port: next(health))
    monkeypatch.setattr(
        start_local,
        "_backend_executable",
        lambda: Path("pythonw-fixture.exe"),
    )
    monkeypatch.setattr(start_local.subprocess, "Popen", popen)
    monkeypatch.setattr(start_local.subprocess, "call", call)

    assert start_local.main(["--install-root", str(root), "--port", "8899"]) == 0
    assert backend_environments[0]["DEEPSEEK_API_KEY"] == expected
    assert "DEEPSEEK_API_KEY" not in client_environments[0]
    assert expected not in client_environments[0].values()
    captured = capsys.readouterr()
    assert expected not in captured.out
    assert expected not in captured.err


def test_launcher_preserves_compatible_llm_environment_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _installation(tmp_path)
    health = iter((False, True, True))
    backend_environments: list[dict[str, str]] = []

    class Process:
        @staticmethod
        def poll():
            return None

    def popen(_command, **kwargs):
        backend_environments.append(kwargs["env"])
        return Process()

    monkeypatch.setattr(start_local, "_health", lambda _port: next(health))
    monkeypatch.setattr(
        start_local,
        "_backend_executable",
        lambda: Path("pythonw-fixture.exe"),
    )
    monkeypatch.setattr(start_local.subprocess, "Popen", popen)
    monkeypatch.setattr(start_local.subprocess, "call", lambda *_args, **_kwargs: 0)
    overrides = {
        "OLIVIA_LLM_BASE_URL": "https://gateway.example/v1",
        "OLIVIA_LLM_MODEL": "compatible-model",
        "OLIVIA_LLM_API_STYLE": "responses",
        "OLIVIA_LLM_STREAM": "false",
        "OLIVIA_LLM_TIMEOUT_SECONDS": "240",
        "OLIVIA_LLM_MAX_RETRIES": "2",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    assert start_local.main(["--install-root", str(root), "--port", "8899"]) == 0

    assert len(backend_environments) == 1
    assert {
        name: backend_environments[0][name]
        for name in overrides
    } == overrides


def test_launcher_supplies_deepseek_defaults_when_llm_overrides_are_absent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _installation(tmp_path)
    health = iter((False, True, True))
    backend_environments: list[dict[str, str]] = []

    class Process:
        @staticmethod
        def poll():
            return None

    def popen(_command, **kwargs):
        backend_environments.append(kwargs["env"])
        return Process()

    monkeypatch.setattr(start_local, "_health", lambda _port: next(health))
    monkeypatch.setattr(
        start_local,
        "_backend_executable",
        lambda: Path("pythonw-fixture.exe"),
    )
    monkeypatch.setattr(start_local.subprocess, "Popen", popen)
    monkeypatch.setattr(start_local.subprocess, "call", lambda *_args, **_kwargs: 0)
    defaults = {
        "OLIVIA_LLM_PROVIDER": "openai_compatible",
        "OLIVIA_LLM_BASE_URL": "https://api.deepseek.com",
        "OLIVIA_LLM_MODEL": "deepseek-v4-flash",
        "OLIVIA_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
        "OLIVIA_LLM_API_STYLE": "chat_completions",
        "OLIVIA_LLM_STREAM": "true",
        "OLIVIA_LLM_TIMEOUT_SECONDS": "180",
        "OLIVIA_LLM_MAX_RETRIES": "0",
    }
    for name in defaults:
        monkeypatch.delenv(name, raising=False)

    assert start_local.main(["--install-root", str(root), "--port", "8899"]) == 0

    assert len(backend_environments) == 1
    assert {
        name: backend_environments[0][name]
        for name in defaults
    } == defaults


def test_launcher_refuses_payload_without_combined_entrypoint(
    tmp_path: Path,
    capsys,
) -> None:
    root = _installation(tmp_path, with_entrypoint=False)

    assert start_local.main(["--install-root", str(root)]) == 2
    assert capsys.readouterr().out.strip() == "PATCH_PAYLOAD_INCOMPLETE"
