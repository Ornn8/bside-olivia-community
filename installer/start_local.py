"""Start the local HTTP server and the isolated copied client."""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def _load_dpapi_key(path: Path) -> str:
    if os.name != "nt" or not path.is_file():
        return ""
    try:
        protected = path.read_text(encoding="utf-8").strip()
        if not protected:
            return ""
        script = "$s=ConvertTo-SecureString ([Console]::In.ReadToEnd()); $b=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try {[Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)} finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b)}"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=protected, text=True, capture_output=True, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, UnicodeError):
        return ""


_CORE_HEALTH_CONTRACT_VERSION = "b02.v1"
_CORE_HEALTH_REQUIRED_CHECKS = (
    "core.health",
    "core.session",
    "letters.read",
    "music.catalog",
)
_CORE_HEALTH_CHECK_STATES = frozenset({"available", "degraded", "unavailable"})


def _health(port: int) -> str:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health?profile=core", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        required_checks = data.get("required_checks") if isinstance(data, dict) else None
        required_checks_match_contract = isinstance(required_checks, dict) and (
            set(required_checks) == set(_CORE_HEALTH_REQUIRED_CHECKS)
        ) and all(
            state in _CORE_HEALTH_CHECK_STATES for state in required_checks.values()
        )
        required_checks_available = required_checks_match_contract and all(
            required_checks.get(name) == "available" for name in _CORE_HEALTH_REQUIRED_CHECKS
        )
        contract_matches = (
            response.status == 200
            and isinstance(payload, dict)
            and type(payload.get("code")) is int
            and payload["code"] == 0
            and payload.get("message") == "ok"
            and isinstance(data, dict)
            and type(data.get("schema_version")) is int
            and data["schema_version"] == 1
            and data.get("contract_version") == _CORE_HEALTH_CONTRACT_VERSION
            and data.get("profile") == "core"
            and data.get("status") in {"HEALTHY", "FAILED"}
            and required_checks_match_contract
            and (data["status"] == "HEALTHY") == required_checks_available
        )
        if not contract_matches:
            return "PORT_CONFLICT"
        return "READY" if data["status"] == "HEALTHY" else "UNAVAILABLE"
    except OSError as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED:
            return "UNAVAILABLE"
        return "PORT_CONFLICT"
    except Exception:
        return "PORT_CONFLICT"


def _client_executable(root: Path) -> Path:
    """Resolve the copied client, never the user's original Steam install."""

    manifest_path = root / "local_backend" / "installer" / "full-patch-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = str(manifest["client_version"])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return root / "app" / "__invalid__" / "Olivia.exe"
    return root / "app" / version / "Olivia.exe"


def _client_command(client: Path, local: Path) -> list[str]:
    """Match the first-party launcher's only client argument."""

    return [str(client), f'--user-data-dir={local / "cef"}']


def _client_environment(environment: dict[str, str], roaming: Path, local: Path) -> dict[str, str]:
    """Isolate profile paths and select the local Steam app without GameId."""

    client_environment = environment.copy()
    client_environment.update(
        {
            "APPDATA": str(roaming),
            "LOCALAPPDATA": str(local),
            "SteamAppId": "4532590",
        }
    )
    client_environment.pop("SteamGameId", None)
    return client_environment


def _backend_executable() -> Path:
    """Prefer the windowless interpreter used by the known-good launcher."""

    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.is_file() else Path(sys.executable)


def _backend_entrypoint(backend: Path) -> Path:
    """Use the one process that mounts toy API and original in-client settings."""

    return backend / "original_client_server.py"


_BACKEND_BOOTSTRAP = (
    "import runpy,sys; "
    "backend,entrypoint,*args=sys.argv[1:]; "
    "sys.path.insert(0, backend); "
    "sys.argv=[entrypoint,*args]; "
    "runpy.run_path(entrypoint,run_name='__main__')"
)


def _memory_enabled(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    return "0" if normalized in {"0", "false", "no", "off"} else "1"


def _configure_memory_environment(
    environment: dict[str, str],
    data_root: Path,
) -> dict[str, str]:
    """Enable the installed Mem0 lifecycle without letting it own other data."""

    memory_root = data_root / "memory"
    environment["OLIVIA_MEMORY_ENABLED"] = _memory_enabled(
        environment.get("OLIVIA_MEMORY_ENABLED")
    )
    environment.setdefault("OLIVIA_MEMORY_DEFAULT_PROVIDER", "mem0")
    environment.setdefault("OLIVIA_MEMORY_ROOT", str(memory_root))
    environment.setdefault(
        "OLIVIA_MEMORY_EMBEDDING_CACHE", str(memory_root / "model-cache")
    )
    environment.setdefault(
        "OLIVIA_MEMORY_LLM_DEFAULT_BASE_URL", environment.get("OLIVIA_LLM_BASE_URL", "")
    )
    environment.setdefault(
        "OLIVIA_MEMORY_LLM_DEFAULT_MODEL", environment.get("OLIVIA_LLM_MODEL", "")
    )
    environment.setdefault(
        "OLIVIA_MEMORY_LLM_DEFAULT_API_KEY_ENV",
        environment.get("OLIVIA_LLM_API_KEY_ENV", "DEEPSEEK_API_KEY"),
    )
    environment.update(
        {
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "MEM0_TELEMETRY": "False",
            "DO_NOT_TRACK": "1",
        }
    )
    return environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=int(os.environ.get("OLIVIA_PORT", "8899")))
    parser.add_argument("--health-only", action="store_true")
    args = parser.parse_args(argv)
    root = args.install_root.expanduser().resolve()
    backend = root / "local_backend"
    entrypoint = _backend_entrypoint(backend)
    if not (backend / "local_server.py").is_file() or not entrypoint.is_file():
        print("PATCH_PAYLOAD_INCOMPLETE")
        return 2
    if args.health_only:
        health = _health(args.port)
        print(json.dumps({"status": health}))
        return 0 if health == "READY" else 2
    health = _health(args.port)
    if health == "PORT_CONFLICT":
        print("PORT_CONFLICT")
        return 2
    data_root = root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    client_environment = os.environ.copy()
    backend_environment = client_environment.copy()
    configured_key = _load_dpapi_key(data_root / "config" / "deepseek_api_key.dpapi")
    if configured_key and not any(backend_environment.get(name) for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")):
        backend_environment["DEEPSEEK_API_KEY"] = configured_key
    runtime_environment = {
        "OLIVIA_INSTALL_ROOT": str(root),
        "OLIVIA_PROJECT_ROOT": str(root / "app"),
        "OLIVIA_LOCAL_DATA_ROOT": str(data_root),
        "OLIVIA_MEMORY_ROOT": str(data_root / "memory"),
        "OLIVIA_PRIVATE_WORLD_ENABLED": "1",
        "OLIVIA_PRIVATE_WORLD_DB": str(
            data_root / "private_world" / "private_world.sqlite3"
        ),
        "OLIVIA_REPLY_DELAY_ENABLED": "1",
        "OLIVIA_REPLY_DELAY_MINUTES_MIN": "5",
        "OLIVIA_REPLY_DELAY_MINUTES_MAX": "10",
        "OLIVIA_PORT": str(args.port),
    }
    backend_environment.update(runtime_environment)
    client_environment.update(runtime_environment)
    for name, value in {
        "OLIVIA_LLM_PROVIDER": "openai_compatible",
        "OLIVIA_LLM_BASE_URL": "https://api.deepseek.com",
        "OLIVIA_LLM_MODEL": "deepseek-v4-flash",
        "OLIVIA_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
        "OLIVIA_LLM_API_STYLE": "chat_completions",
        "OLIVIA_LLM_STREAM": "true",
        "OLIVIA_LLM_TIMEOUT_SECONDS": "180",
        "OLIVIA_LLM_MAX_RETRIES": "0",
    }.items():
        backend_environment.setdefault(name, value)
        client_environment.setdefault(name, value)
    _configure_memory_environment(backend_environment, data_root)
    if not any(backend_environment.get(name) for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")):
        print("LLM_API_KEY_NOT_CONFIGURED: 请先在启动此程序的进程环境中设置 API key；当前仅提供明确的 safe-static/degraded 回退。")
    server = None
    if health != "READY":
        detached = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        server = subprocess.Popen(
            [
                str(_backend_executable()),
                "-c",
                _BACKEND_BOOTSTRAP,
                str(backend),
                str(entrypoint),
            ],
            cwd=backend,
            env=backend_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=detached,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and server.poll() is None:
            if _health(args.port) == "READY":
                break
            time.sleep(0.25)
        health = _health(args.port)
        if health != "READY":
            if health == "PORT_CONFLICT":
                print("PORT_CONFLICT")
                return 2
            print("LOCAL_SERVER_UNAVAILABLE")
            return 2
    client = _client_executable(root)
    if not client.is_file():
        print("ISOLATED_CLIENT_NOT_FOUND")
        return 2
    profile = root / "profile"
    roaming = profile / "Roaming"
    local = profile / "Local"
    roaming.mkdir(parents=True, exist_ok=True)
    local.mkdir(parents=True, exist_ok=True)
    return subprocess.call(_client_command(client, local), cwd=root / "app", env=_client_environment(client_environment, roaming, local))


if __name__ == "__main__":
    raise SystemExit(main())
