from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
import zipfile

import pytest
from jsonschema import Draft202012Validator

import pytest

from installer import configure
import installer.start_local as start_local
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT, SETTINGS_UI_VERSION
from original_client_setup_api import _dpapi_protect
from patch_companion_settings import CompanionSettingsPatchError


def _installation(
    tmp_path: Path,
    *,
    with_entrypoint: bool = True,
    client_version: str = "0.0.9.615",
) -> Path:
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
        json.dumps({"client_version": client_version}),
        encoding="utf-8",
    )
    client = root / "app" / client_version / "Olivia.exe"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"fixture")
    resources = client.parent / "resources"
    resources.mkdir()
    main_member = (
        "assets/main-31595bd3.js"
        if client_version == "0.0.9.627"
        else "assets/main-917d29fc.js"
    )
    index = (
        '<!doctype html><html><head>'
        f'<script type="module" src="./{main_member}"></script>'
        '<script src="./assets/olivia-companion-settings.js" '
        'data-olivia-companion-settings="p03.original-settings-shell.v1" '
        f'data-ui-version="{SETTINGS_UI_VERSION}" '
        'data-api-base="http://127.0.0.1:8899/"></script>'
        '</head><body><div id="app"></div></body></html>'
    )
    main = (
        b'synthetic-main"hide-write":!1'
        if client_version == "0.0.9.627"
        else b"synthetic-main"
    )
    with zipfile.ZipFile(resources / "feapp.dat", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", index)
        archive.writestr(main_member, main)
        archive.writestr("assets/olivia-companion-settings.js", BOOTSTRAP_JAVASCRIPT)
    return root


def test_launcher_repairs_existing_0627_frontend_before_start(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path, client_version="0.0.9.627")
    resources = root / "app" / "0.0.9.627" / "resources"
    index = """<!doctype html><html><head>
<script type="module" src="./assets/main-31595bd3.js"></script>
<script src="./assets/olivia-companion-settings.js" data-olivia-companion-settings="p03.original-settings-shell.v1" data-ui-version="p03.original-settings-manage.v6" data-api-base="http://127.0.0.1:8899/"></script>
</head><body><div id="app"></div></body></html>"""
    old_bootstrap = BOOTSTRAP_JAVASCRIPT.replace(
        '    document.querySelector(`[${ROOT_ATTR}]`)?.remove();\n',
        '    document.querySelector(`[${ROOT_ATTR}]`)?.remove();\n'
        '    document.querySelector(`[${DIALOG_ATTR}]`)?.remove();\n',
        1,
    )
    feapp = resources / "feapp.dat"
    with zipfile.ZipFile(feapp, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", index)
        archive.writestr(
            "assets/main-31595bd3.js",
            b'prefix"hide-write":o(p)||!o(N3)suffix',
        )
        archive.writestr("assets/olivia-companion-settings.js", old_bootstrap)

    assert start_local._repair_client_frontend(root, 8899) == "PATCHED"

    with zipfile.ZipFile(feapp) as archive:
        bootstrap = archive.read("assets/olivia-companion-settings.js").decode()
        main = archive.read("assets/main-31595bd3.js").decode()
    assert bootstrap == BOOTSTRAP_JAVASCRIPT
    assert '"hide-write":!1' in main


def test_launcher_rejects_missing_frontend_archive(tmp_path: Path) -> None:
    root = _installation(tmp_path, client_version="0.0.9.627")
    (root / "app" / "0.0.9.627" / "resources" / "feapp.dat").unlink()

    with pytest.raises(CompanionSettingsPatchError) as error:
        start_local._repair_client_frontend(root, 8899)

    assert error.value.code == "COMPANION_ARCHIVE_NOT_FOUND"


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
    frontend_repairs: list[tuple[Path, int]] = []

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
    monkeypatch.setattr(start_local, "_active_backend", lambda: root / "local_backend")
    monkeypatch.setattr(
        start_local,
        "_backend_executable",
        lambda: Path("pythonw-fixture.exe"),
    )
    monkeypatch.setattr(start_local.subprocess, "Popen", popen)
    monkeypatch.setattr(start_local.subprocess, "call", call)
    monkeypatch.setattr(
        start_local,
        "_repair_client_frontend",
        lambda installation, port: frontend_repairs.append((installation, port))
        or "PATCHED",
    )

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
    assert frontend_repairs == [(root.resolve(), 8899)]


def test_component_launcher_starts_the_backend_that_owns_start_local(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _installation(tmp_path)
    active_backend = tmp_path / "versions" / "local_backend" / "0.1.2-digest"
    (active_backend / "installer").mkdir(parents=True)
    (active_backend / "local_server.py").write_text("# active", encoding="utf-8")
    active_entrypoint = active_backend / "original_client_server.py"
    active_entrypoint.write_text("# active", encoding="utf-8")
    health = iter(("UNAVAILABLE", "READY", "READY"))
    commands: list[list[str]] = []

    class Process:
        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(start_local, "_active_backend", lambda: active_backend)
    monkeypatch.setattr(start_local, "_health", lambda _port: next(health))
    monkeypatch.setattr(start_local, "_backend_executable", lambda: Path("pythonw.exe"))
    monkeypatch.setattr(
        start_local.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(
            [str(value) for value in command]
        )
        or Process(),
    )
    monkeypatch.setattr(start_local.subprocess, "call", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        start_local,
        "_repair_client_frontend",
        lambda *_args: "PATCHED",
    )

    assert start_local.main(["--install-root", str(root), "--port", "8899"]) == 0
    assert commands[0][3:] == [str(active_backend), str(active_entrypoint)]


def test_component_launcher_identifies_and_stops_its_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _installation(tmp_path)
    active_backend = tmp_path / "versions" / "local_backend" / "0.1.2-digest"
    (active_backend / "installer").mkdir(parents=True)
    (active_backend / "local_server.py").write_text("# active", encoding="utf-8")
    (active_backend / "original_client_server.py").write_text(
        "# active",
        encoding="utf-8",
    )
    health = iter(("UNAVAILABLE", "READY", "READY"))
    backend_environments: list[dict[str, str]] = []
    lifecycle: list[str] = []

    class Process:
        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate() -> None:
            lifecycle.append("terminate")

        @staticmethod
        def wait(*, timeout: float) -> int:
            lifecycle.append(f"wait:{timeout}")
            return 0

    def popen(_command, **kwargs):
        backend_environments.append(kwargs["env"])
        return Process()

    monkeypatch.setattr(start_local, "_active_backend", lambda: active_backend)
    monkeypatch.setattr(start_local, "_health", lambda _port: next(health))
    monkeypatch.setattr(start_local.subprocess, "Popen", popen)
    monkeypatch.setattr(start_local.subprocess, "call", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(start_local, "_repair_client_frontend", lambda *_args: "PATCHED")

    assert start_local.main(["--install-root", str(root), "--port", "8899"]) == 0
    assert backend_environments[0]["OLIVIA_BACKEND_ID"] == start_local._backend_id(
        active_backend,
        root.resolve(),
    )
    assert backend_environments[0]["OLIVIA_PROVIDER_CACHE_ROOT"] == str(
        root / "data" / "provider-cache"
    )
    assert lifecycle == ["terminate", "wait:5"]


def test_launcher_replaces_a_verified_stale_backend_before_starting_active_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _installation(tmp_path)
    active_backend = tmp_path / "versions" / "local_backend" / "0.1.2-digest"
    (active_backend / "installer").mkdir(parents=True)
    (active_backend / "local_server.py").write_text("# active", encoding="utf-8")
    (active_backend / "original_client_server.py").write_text(
        "# active",
        encoding="utf-8",
    )
    health = iter(("READY", "UNAVAILABLE", "READY", "READY"))
    stale_stops: list[tuple[int, Path]] = []
    backend_starts: list[list[str]] = []

    class Process:
        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(start_local, "_active_backend", lambda: active_backend)
    monkeypatch.setattr(start_local, "_health", lambda _port: next(health))
    monkeypatch.setattr(start_local, "_server_backend_id", lambda _port: "legacy")
    monkeypatch.setattr(
        start_local,
        "_stop_stale_backend",
        lambda port, installation: stale_stops.append((port, installation)) or True,
    )
    monkeypatch.setattr(
        start_local.subprocess,
        "Popen",
        lambda command, **_kwargs: backend_starts.append(command) or Process(),
    )
    monkeypatch.setattr(start_local.subprocess, "call", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(start_local, "_repair_client_frontend", lambda *_args: "PATCHED")

    assert start_local.main(["--install-root", str(root), "--port", "8899"]) == 0
    assert stale_stops == [(8899, root.resolve())]
    assert len(backend_starts) == 1


def test_launcher_reads_backend_identity_from_health_response(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read() -> bytes:
            return json.dumps(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {"backend_id": "0.1.2-digest"},
                }
            ).encode("utf-8")

    monkeypatch.setattr(start_local, "urlopen", lambda *_args, **_kwargs: Response())

    assert start_local._server_backend_id(8899) == "0.1.2-digest"


def test_backend_identity_tracks_version_and_installation(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    legacy = first_root / "local_backend"
    versioned = tmp_path / "versions" / "local_backend" / "0.1.2-digest"

    legacy_id = start_local._backend_id(legacy, first_root)
    versioned_id = start_local._backend_id(versioned, first_root)

    assert legacy_id.startswith("legacy.")
    assert versioned_id.startswith("0.1.2-digest.")
    assert start_local._backend_id(versioned, second_root) != versioned_id


def test_stale_backend_stop_requires_runtime_owned_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "installed"
    calls: list[tuple[int, Path]] = []
    monkeypatch.setattr(start_local, "_listening_process_id", lambda _port: 4321)
    monkeypatch.setattr(
        start_local,
        "_terminate_runtime_process",
        lambda pid, installation: calls.append((pid, installation)) or True,
    )

    assert start_local._stop_stale_backend(8899, root) is True
    assert calls == [(4321, root)]


def test_runtime_process_ownership_is_scoped_to_one_installation(tmp_path: Path) -> None:
    root = tmp_path / "product" / "install"
    owned = tmp_path / "product" / "runtime" / "python-3.12" / "pythonw.exe"
    another = tmp_path / "other" / "runtime" / "python-3.12" / "pythonw.exe"
    unrelated = tmp_path / "product" / "runtime" / "Olivia.exe"

    assert start_local._runtime_owns_executable(owned, root) is True
    assert start_local._runtime_owns_executable(another, root) is False
    assert start_local._runtime_owns_executable(unrelated, root) is False


def test_launcher_allows_mem0_cold_start_before_opening_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _installation(tmp_path)
    clock = [0.0]
    client_commands: list[list[str]] = []

    class Process:
        @staticmethod
        def poll():
            return None

    def health(_port: int) -> str:
        return "READY" if clock[0] >= 45 else "UNAVAILABLE"

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def call(command, **_kwargs):
        client_commands.append([str(value) for value in command])
        return 0

    monkeypatch.setattr(start_local, "_health", health)
    monkeypatch.setattr(start_local.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(start_local.time, "sleep", sleep)
    monkeypatch.setattr(start_local.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(start_local.subprocess, "call", call)

    assert start_local.main(["--install-root", str(root), "--port", "8899"]) == 0
    assert clock[0] >= 45
    assert len(client_commands) == 1


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
    monkeypatch,
    capsys,
) -> None:
    root = _installation(tmp_path, with_entrypoint=False)
    monkeypatch.setattr(start_local, "_active_backend", lambda: root / "local_backend")

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
    monkeypatch.setattr(start_local, "_port_is_bindable", lambda _port: True)

    assert start_local._health(8899) == "UNAVAILABLE"


def test_health_treats_windows_connection_refused_as_no_listener(monkeypatch) -> None:
    class WindowsConnectionRefused(OSError):
        errno = None
        winerror = 10061

    def connection_refused(*_args, **_kwargs):
        raise URLError(WindowsConnectionRefused("connection refused"))

    monkeypatch.setattr(start_local, "urlopen", connection_refused)
    monkeypatch.setattr(start_local, "_port_is_bindable", lambda _port: True)

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
