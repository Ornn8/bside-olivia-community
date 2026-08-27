from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from jsonschema import Draft202012Validator

import pytest

from installer import configure
import installer.start_local as start_local
from original_client_setup_api import _dpapi_protect


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


def test_launcher_loads_user_managed_llm_config_without_exposing_key(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    config_root = data_root / "config"
    config_root.mkdir(parents=True)
    (config_root / "llm.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": "https://opencode.ai/zen/go/v1",
                "model": "deepseek-v4-flash",
            }
        ),
        encoding="utf-8",
    )

    environment = start_local._load_llm_environment({}, data_root)

    assert environment["OLIVIA_LLM_BASE_URL"] == "https://opencode.ai/zen/go/v1"
    assert environment["OLIVIA_LLM_MODEL"] == "deepseek-v4-flash"
    assert environment["OLIVIA_LLM_API_KEY_ENV"] == "DEEPSEEK_API_KEY"


def test_launcher_ignores_invalid_user_managed_llm_config(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    config_root = data_root / "config"
    config_root.mkdir(parents=True)
    (config_root / "llm.json").write_text(
        '{"schema_version":1,"base_url":"http://remote.example/v1","model":"bad model"}',
        encoding="utf-8",
    )
    (config_root / "deepseek_api_key.dpapi").write_text(
        "synthetic-ciphertext\n", encoding="utf-8"
    )

    environment = start_local._load_llm_environment(
        {}, data_root, include_secret=True
    )

    assert environment["OLIVIA_LLM_BASE_URL"] == "https://api.deepseek.com"
    assert environment["OLIVIA_LLM_MODEL"] == "deepseek-v4-flash"
    assert environment["OLIVIA_LLM_PROVIDER"] == "none"
    assert "DEEPSEEK_API_KEY" not in environment


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is required")
def test_launcher_reads_current_user_dpapi_key_format(tmp_path: Path) -> None:
    key_path = tmp_path / "deepseek_api_key.dpapi"
    key_path.write_text(_dpapi_protect("synthetic-launcher-key") + "\n", encoding="utf-8")

    assert start_local._load_dpapi_key(key_path) == "synthetic-launcher-key"


def test_launcher_starts_combined_server_before_original_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _installation(tmp_path)
    health = iter(("UNAVAILABLE", "READY", "READY"))
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

    health = iter(("UNAVAILABLE", "READY", "READY"))
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
    health = iter(("UNAVAILABLE", "READY", "READY"))
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
    health = iter(("UNAVAILABLE", "READY", "READY"))
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


def test_launcher_refuses_unrelated_http_health_payload(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = _installation(tmp_path)
    for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(start_local, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(
        start_local.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "launcher must not start a backend when the port serves an unrelated JSON payload"
        ),
    )

    assert start_local.main(["--install-root", str(root)]) == 2
    assert capsys.readouterr().out.strip() == "PORT_CONFLICT"


def test_launcher_refuses_http_error_without_starting_backend_or_client(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = _installation(tmp_path)
    for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    backend_commands: list[list[str]] = []
    client_commands: list[list[str]] = []

    class Process:
        @staticmethod
        def poll():
            return 0

    def raise_not_found(*_args, **_kwargs):
        raise HTTPError(
            "http://127.0.0.1:8899/health?profile=core",
            404,
            "Not Found",
            None,
            None,
        )

    def popen(command, **_kwargs):
        backend_commands.append([str(value) for value in command])
        return Process()

    def call(command, **_kwargs):
        client_commands.append([str(value) for value in command])
        return 0

    monkeypatch.setattr(start_local, "urlopen", raise_not_found)
    monkeypatch.setattr(start_local.subprocess, "Popen", popen)
    monkeypatch.setattr(start_local.subprocess, "call", call)

    assert start_local.main(["--install-root", str(root)]) == 2
    assert capsys.readouterr().out.strip() == "PORT_CONFLICT"
    assert backend_commands == []
    assert client_commands == []


def test_launcher_refuses_inconsistent_failed_health_before_side_effects(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = _installation(tmp_path)
    for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    backend_commands: list[list[str]] = []
    client_commands: list[list[str]] = []

    class Process:
        @staticmethod
        def poll():
            return 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "schema_version": 1,
                        "contract_version": "b02.v1",
                        "profile": "core",
                        "status": "FAILED",
                        "required_checks": {
                            "core.health": "available",
                            "core.session": "available",
                            "letters.read": "available",
                            "music.catalog": "available",
                        },
                    },
                }
            ).encode("utf-8")

    def popen(command, **_kwargs):
        backend_commands.append([str(value) for value in command])
        return Process()

    def call(command, **_kwargs):
        client_commands.append([str(value) for value in command])
        return 0

    monkeypatch.setattr(start_local, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(start_local.subprocess, "Popen", popen)
    monkeypatch.setattr(start_local.subprocess, "call", call)

    assert start_local.main(["--install-root", str(root)]) == 2
    assert capsys.readouterr().out.strip() == "PORT_CONFLICT"
    assert not (root / "data").exists()
    assert backend_commands == []
    assert client_commands == []


@pytest.mark.parametrize(
    ("code", "schema_version"),
    [(False, 1), (0, True)],
    ids=("boolean-code", "boolean-schema-version"),
)
def test_launcher_refuses_boolean_health_contract_integers_before_side_effects(
    tmp_path: Path,
    monkeypatch,
    capsys,
    code: object,
    schema_version: object,
) -> None:
    root = _installation(tmp_path)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "code": code,
                    "message": "ok",
                    "data": {
                        "schema_version": schema_version,
                        "contract_version": "b02.v1",
                        "profile": "core",
                        "status": "HEALTHY",
                        "required_checks": {
                            "core.health": "available",
                            "core.session": "available",
                            "letters.read": "available",
                            "music.catalog": "available",
                        },
                    },
                }
            ).encode("utf-8")

    monkeypatch.setattr(start_local, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(
        start_local.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("boolean contract values must not start a backend"),
    )
    monkeypatch.setattr(
        start_local.subprocess,
        "call",
        lambda *_args, **_kwargs: pytest.fail("boolean contract values must not start a client"),
    )

    assert start_local.main(["--install-root", str(root)]) == 2
    assert capsys.readouterr().out.strip() == "PORT_CONFLICT"
    assert not (root / "data").exists()


@pytest.mark.parametrize(
    ("status", "required_checks"),
    [
        ("FAILED", {}),
        (
            "FAILED",
            {
                "core.health": "available",
                "core.session": "available",
                "letters.read": "available",
            },
        ),
        (
            "HEALTHY",
            {
                "core.health": "available",
                "core.session": "available",
                "letters.read": "available",
                "music.catalog": "available",
                "unexpected.check": "available",
            },
        ),
        (
            "FAILED",
            {
                "core.health": "unknown",
                "core.session": "available",
                "letters.read": "available",
                "music.catalog": "available",
            },
        ),
    ],
    ids=("empty", "missing", "extra", "invalid-state"),
)
def test_launcher_refuses_noncanonical_core_required_checks_before_side_effects(
    tmp_path: Path,
    monkeypatch,
    capsys,
    status: str,
    required_checks: dict[str, str],
) -> None:
    root = _installation(tmp_path)
    for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "schema_version": 1,
                        "contract_version": "b02.v1",
                        "profile": "core",
                        "status": status,
                        "required_checks": required_checks,
                    },
                }
            ).encode("utf-8")

    monkeypatch.setattr(start_local, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(
        start_local.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "launcher must not start a backend for a noncanonical core health payload"
        ),
    )
    monkeypatch.setattr(
        start_local.subprocess,
        "call",
        lambda *_args, **_kwargs: pytest.fail(
            "launcher must not start a client for a noncanonical core health payload"
        ),
    )

    assert start_local.main(["--install-root", str(root)]) == 2
    assert capsys.readouterr().out.strip() == "PORT_CONFLICT"
    assert not (root / "data").exists()


@pytest.mark.parametrize(
    ("status", "required_checks", "expected"),
    [
        (
            "HEALTHY",
            {
                "core.health": "available",
                "core.session": "available",
                "letters.read": "available",
                "music.catalog": "available",
            },
            "READY",
        ),
        (
            "FAILED",
            {
                "core.health": "degraded",
                "core.session": "available",
                "letters.read": "available",
                "music.catalog": "available",
            },
            "UNAVAILABLE",
        ),
    ],
    ids=("healthy", "failed-degraded"),
)
def test_health_accepts_the_versioned_core_contract(
    monkeypatch,
    status: str,
    required_checks: dict[str, str],
    expected: str,
) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "schema_version": 1,
                        "contract_version": "b02.v1",
                        "profile": "core",
                        "status": status,
                        "required_checks": required_checks,
                    },
                }
            ).encode("utf-8")

    monkeypatch.setattr(start_local, "urlopen", lambda *_args, **_kwargs: Response())

    assert start_local._health(8899) == expected


def test_health_treats_connection_refused_as_no_listener(monkeypatch) -> None:
    def connection_refused(*_args, **_kwargs):
        raise URLError(ConnectionRefusedError(errno.ECONNREFUSED, "connection refused"))

    monkeypatch.setattr(start_local, "urlopen", connection_refused)

    assert start_local._health(8899) == "UNAVAILABLE"


def test_health_treats_windows_connection_refused_as_no_listener(monkeypatch) -> None:
    class WindowsConnectionRefused(OSError):
        errno = None
        winerror = 10061

    def connection_refused(*_args, **_kwargs):
        raise URLError(WindowsConnectionRefused("connection refused"))

    monkeypatch.setattr(start_local, "urlopen", connection_refused)

    assert start_local._health(8899) == "UNAVAILABLE"


@pytest.mark.parametrize(
    ("bindable", "expected"),
    [(True, "UNAVAILABLE"), (False, "PORT_CONFLICT")],
    ids=("bindable", "occupied"),
)
def test_health_resolves_timeout_by_actual_local_port_bindability(
    monkeypatch,
    bindable: bool,
    expected: str,
) -> None:
    def timed_out(*_args, **_kwargs):
        raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr(start_local, "urlopen", timed_out)
    monkeypatch.setattr(start_local, "_port_is_bindable", lambda _port: bindable)

    assert start_local._health(8899) == expected


def test_health_only_reports_port_conflict_using_the_public_cli_schema(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = _installation(tmp_path)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(start_local, "urlopen", lambda *_args, **_kwargs: Response())

    assert start_local.main(["--install-root", str(root), "--health-only"]) == 2
    result = json.loads(capsys.readouterr().out)
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts" / "launcher_health.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert result == {"status": "PORT_CONFLICT"}
    assert not list(Draft202012Validator(schema).iter_errors(result))
