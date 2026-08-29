"""Start the local HTTP server and the isolated copied client."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from patch_companion_settings import (
    CompanionSettingsPatchError,
    patch_companion_settings,
)
from video_capability_install import (
    VideoCapabilityError,
    load_video_runtime_environment,
)


def _load_dpapi_key(path: Path) -> str:
    if os.name != "nt" or not path.is_file():
        return ""
    try:
        protected = path.read_text(encoding="utf-8").strip()
        if not protected:
            return ""
        modern = protected.startswith("dpapi-v1:")
        script = (
            "Add-Type -AssemblyName System.Security; "
            "$p=[Convert]::FromBase64String([Console]::In.ReadToEnd()); "
            "$b=[Security.Cryptography.ProtectedData]::Unprotect($p,$null,"
            "[Security.Cryptography.DataProtectionScope]::CurrentUser); "
            "[Text.Encoding]::UTF8.GetString($b)"
            if modern
            else "$s=ConvertTo-SecureString ([Console]::In.ReadToEnd()); $b=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try {[Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)} finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b)}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=protected.removeprefix("dpapi-v1:"),
            text=True,
            capture_output=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, UnicodeError):
        return ""


_LLM_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _load_video_environment(
    environment: dict[str, str], data_root: Path
) -> dict[str, str]:
    """Load only verified paths persisted by the video bundle assembler."""

    values = environment.copy()
    try:
        persisted = load_video_runtime_environment(data_root.resolve())
    except (OSError, TypeError, ValueError, VideoCapabilityError):
        return values
    for key, value in persisted.items():
        values.setdefault(key, value)
    backend_tools = Path(__file__).resolve().parents[1] / "tools"
    minimax_worker = backend_tools / "minimax_music3_worker.py"
    minimax_profile = backend_tools / "minimax_profile.py"
    if minimax_worker.is_file() and minimax_profile.is_file():
        values["OLIVIA_MINIMAX_WORKER"] = str(minimax_worker)
    return values


def _load_llm_environment(
    environment: dict[str, str],
    data_root: Path,
    *,
    include_secret: bool = False,
) -> dict[str, str]:
    """Load public provider settings while retaining safe defaults on corruption."""

    values = environment.copy()
    base_url = "https://api.deepseek.com"
    model = "deepseek-v4-flash"
    provider = "openai_compatible"
    key_path = data_root / "config" / "deepseek_api_key.dpapi"
    config_path = data_root / "config" / "llm.json"
    saved_key_binding = False
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        candidate_url = str(payload["base_url"]).strip().rstrip("/")
        candidate_model = str(payload["model"]).strip()
        parsed = urlsplit(candidate_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            payload.get("schema_version") in {1, 2}
            and parsed.hostname
            and parsed.scheme in ({"http", "https"} if loopback else {"https"})
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and _LLM_MODEL_RE.fullmatch(candidate_model)
        ):
            base_url = candidate_url
            model = candidate_model
            if payload.get("schema_version") == 2:
                name = payload.get("key_file")
                expected_hash = payload.get("key_sha256")
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(
                        r"deepseek_api_key\.[0-9a-f]{32}\.dpapi", name
                    )
                    or not isinstance(expected_hash, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
                ):
                    raise ValueError("invalid key binding")
                key_path = config_path.parent / name
                if hashlib.sha256(key_path.read_bytes()).hexdigest() != expected_hash:
                    raise ValueError("invalid key binding")
                saved_key_binding = True
        else:
            raise ValueError("invalid LLM config")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        provider = "none"
        key_path = Path()
    for name, value in {
        "OLIVIA_LLM_PROVIDER": provider,
        "OLIVIA_LLM_BASE_URL": base_url,
        "OLIVIA_LLM_MODEL": model,
        "OLIVIA_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
        "OLIVIA_LLM_API_STYLE": "chat_completions",
        "OLIVIA_LLM_STREAM": "true",
        "OLIVIA_LLM_TIMEOUT_SECONDS": "180",
        "OLIVIA_LLM_MAX_RETRIES": "0",
    }.items():
        values.setdefault(name, value)
    if values.get("OLIVIA_LLM_API_KEY"):
        values["OLIVIA_LLM_API_KEY_ENV"] = "OLIVIA_LLM_API_KEY"
    generic_key_present = any(
        values.get(name) for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY")
    )
    # A schema-v2 binding is the user's explicit saved choice; inherited generic
    # keys must not outrank it.  The Olivia-specific override remains explicit.
    if (
        include_secret
        and key_path != Path()
        and not values.get("OLIVIA_LLM_API_KEY")
        and (saved_key_binding or not generic_key_present)
    ):
        configured_key = _load_dpapi_key(key_path)
        if configured_key:
            values["DEEPSEEK_API_KEY"] = configured_key
    return values


_CORE_HEALTH_CONTRACT_VERSION = "b02.v1"
_CORE_HEALTH_REQUIRED_CHECKS = (
    "core.health",
    "core.session",
    "letters.read",
    "music.catalog",
)
_CORE_HEALTH_CHECK_STATES = frozenset({"available", "degraded", "unavailable"})
_BACKEND_START_TIMEOUT_SECONDS = 120
_BACKEND_ID_RE = re.compile(r"[0-9A-Za-z.+-]{1,160}")


def _port_is_bindable(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


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
    except HTTPError:
        return "PORT_CONFLICT"
    except Exception:
        return "UNAVAILABLE" if _port_is_bindable(port) else "PORT_CONFLICT"


def _backend_id(backend: Path, root: Path) -> str:
    """Return a path-free identity for the selected backend tree."""

    if backend.name == "local_backend":
        component_id = "legacy"
    elif (
        backend.parent.name == "local_backend"
        and backend.parent.parent.name == "versions"
        and _BACKEND_ID_RE.fullmatch(backend.name)
    ):
        component_id = backend.name
    else:
        component_id = "invalid"
    installation_id = hashlib.sha256(
        os.path.normcase(os.fspath(root)).encode("utf-8")
    ).hexdigest()[:16]
    return f"{component_id}.{installation_id}"


def _server_backend_id(port: int) -> str | None:
    """Read the path-free identity published by the backend on this port."""

    try:
        with urlopen(
            f"http://127.0.0.1:{port}/health?profile=core",
            timeout=1.5,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        backend_id = data.get("backend_id") if isinstance(data, dict) else None
        if (
            response.status == 200
            and payload.get("code") == 0
            and isinstance(backend_id, str)
            and _BACKEND_ID_RE.fullmatch(backend_id)
        ):
            return backend_id
    except Exception:
        pass
    return None


def _stop_backend_server(server: object) -> None:
    """Best-effort cleanup for the backend process owned by this launcher."""

    terminate = getattr(server, "terminate", None)
    wait = getattr(server, "wait", None)
    if not callable(terminate) or not callable(wait):
        return
    try:
        terminate()
        wait(timeout=5)
    except subprocess.TimeoutExpired:
        kill = getattr(server, "kill", None)
        if callable(kill):
            try:
                kill()
                wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
    except OSError:
        pass


def _listening_process_id(port: int) -> int | None:
    """Resolve the Windows PID that owns one listening TCP port."""

    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class TcpRowOwnerPid(ctypes.Structure):
        _fields_ = [
            ("state", wintypes.DWORD),
            ("local_address", wintypes.DWORD),
            ("local_port", wintypes.DWORD),
            ("remote_address", wintypes.DWORD),
            ("remote_port", wintypes.DWORD),
            ("process_id", wintypes.DWORD),
        ]

    size = wintypes.ULONG()
    table = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    table(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    if not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if table(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0) != 0:
        return None
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    offset = ctypes.sizeof(wintypes.DWORD)
    row_size = ctypes.sizeof(TcpRowOwnerPid)
    for index in range(count):
        row = TcpRowOwnerPid.from_buffer_copy(buffer, offset + index * row_size)
        if socket.ntohs(row.local_port & 0xFFFF) == port:
            return int(row.process_id)
    return None


def _runtime_owns_executable(executable: Path, root: Path) -> bool:
    """Limit legacy migration to Python bundled beside this installation."""

    if executable.name.casefold() not in {"python.exe", "pythonw.exe"}:
        return False
    candidate = os.path.normcase(os.fspath(executable.absolute()))
    for runtime in (root.parent / "runtime", root / "runtime"):
        boundary = os.path.normcase(os.fspath(runtime.absolute()))
        try:
            if os.path.commonpath((candidate, boundary)) == boundary:
                return True
        except ValueError:
            continue
    return False


def _terminate_runtime_process(process_id: int, root: Path) -> bool:
    """Terminate a listener only when its executable belongs to this install."""

    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    process_access = 0x0001 | 0x1000 | 0x00100000
    handle = kernel32.OpenProcess(process_access, False, process_id)
    if not handle:
        return False
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(length),
        ) or not _runtime_owns_executable(Path(buffer.value), root):
            return False
        if not kernel32.TerminateProcess(handle, 0):
            return False
        return kernel32.WaitForSingleObject(handle, 5000) == 0
    finally:
        kernel32.CloseHandle(handle)


def _stop_stale_backend(port: int, root: Path) -> bool:
    """Stop a legacy listener only after proving it uses this install runtime."""

    process_id = _listening_process_id(port)
    return process_id is not None and _terminate_runtime_process(process_id, root)


def _client_executable(root: Path) -> Path:
    """Resolve the copied client, never the user's original Steam install."""

    manifest_path = root / "local_backend" / "installer" / "full-patch-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = str(manifest["client_version"])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return root / "app" / "__invalid__" / "Olivia.exe"
    return root / "app" / version / "Olivia.exe"


def _load_fixed_video_assets_environment(
    environment: dict[str, str], root: Path
) -> dict[str, str]:
    """Use the one fixed scene already shipped with the isolated Olivia client."""

    values = environment.copy()
    assets = _client_executable(root).parent / "assets" / "Wallpaper_Presence"
    scene = assets / "A_R1_2000.mp4"
    transition = assets / "A_Transition_2000_1200.mp4"
    if not scene.is_file() or not transition.is_file():
        return values
    values.setdefault("OLIVIA_ORDINARY_ACTION_BASE", str(scene.resolve()))
    values.setdefault("OLIVIA_OFFICIAL_REPLY_REFERENCE", str(transition.resolve()))
    values.setdefault("OLIVIA_MUSIC_PERFORMANCE_BASE", str(scene.resolve()))
    return values


def _repair_client_frontend(root: Path, port: int) -> str:
    """Upgrade repository-owned UI in an existing isolated client copy."""

    client = _client_executable(root)
    feapp = client.parent / "resources" / "feapp.dat"
    if not feapp.is_file():
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_NOT_FOUND")
    result = patch_companion_settings(
        feapp,
        f"http://127.0.0.1:{port}/",
        work_root=feapp.parent,
    )
    return result["status"]


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


def _append_launcher_event(data_root: Path, event: str, **fields: object) -> None:
    """Persist path-free startup diagnostics without environment or credentials."""

    try:
        log_root = data_root / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        record = {"event": event, **fields}
        with (log_root / "launcher.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _active_backend() -> Path:
    """Resolve the complete backend tree that owns this launcher module."""

    return Path(__file__).resolve().parents[1]


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


def _memory_write_timeout(environment: dict[str, str]) -> str:
    try:
        value = float(environment.get("OLIVIA_LLM_TIMEOUT_SECONDS", "180"))
    except (TypeError, ValueError):
        value = 180.0
    if not math.isfinite(value):
        value = 180.0
    return format(min(300.0, max(0.1, value)), "g")


def _start_backend_server(
    *,
    backend: Path,
    entrypoint: Path,
    environment: dict[str, str],
    port: int,
    data_root: Path,
    expected_backend_id: str,
) -> tuple[object, str]:
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    _append_launcher_event(data_root, "backend_start")
    server = subprocess.Popen(
        [
            str(_backend_executable()),
            "-c",
            _BACKEND_BOOTSTRAP,
            str(backend),
            str(entrypoint),
        ],
        cwd=backend,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + _BACKEND_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server.poll() is not None:
            break
        if (
            _health(port) == "READY"
            and _server_backend_id(port) == expected_backend_id
        ):
            break
        time.sleep(0.25)
    health = _health(port)
    if (
        server.poll() is None
        and health == "READY"
        and _server_backend_id(port) == expected_backend_id
    ):
        _append_launcher_event(data_root, "backend_ready")
    else:
        if health == "READY":
            health = "UNAVAILABLE"
        _append_launcher_event(
            data_root,
            "backend_unavailable",
            health=health,
            exit_code=server.poll(),
        )
    return server, health


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
    environment.setdefault(
        "OLIVIA_MEMORY_WRITE_TIMEOUT_SECONDS",
        _memory_write_timeout(environment),
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
    backend = _active_backend()
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
    expected_backend_id = _backend_id(backend, root)
    if health == "READY" and _server_backend_id(args.port) != expected_backend_id:
        if not _stop_stale_backend(args.port, root):
            print("STALE_BACKEND_RUNNING")
            return 2
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            health = _health(args.port)
            if health == "UNAVAILABLE":
                break
            time.sleep(0.1)
        if health != "UNAVAILABLE":
            print("STALE_BACKEND_RUNNING")
            return 2
    data_root = root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    client_environment = os.environ.copy()
    backend_environment = client_environment.copy()
    runtime_environment = {
        "OLIVIA_INSTALL_ROOT": str(root),
        "OLIVIA_PROJECT_ROOT": str(root / "app"),
        "OLIVIA_LOCAL_DATA_ROOT": str(data_root),
        "OLIVIA_PROVIDER_CACHE_ROOT": str(data_root / "provider-cache"),
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
    backend_environment["OLIVIA_BACKEND_ID"] = expected_backend_id
    client_environment.update(runtime_environment)
    backend_environment = _load_video_environment(backend_environment, data_root)
    client_environment = _load_video_environment(client_environment, data_root)
    backend_environment = _load_fixed_video_assets_environment(backend_environment, root)
    client_environment = _load_fixed_video_assets_environment(client_environment, root)
    backend_environment = _load_llm_environment(
        backend_environment, data_root, include_secret=True
    )
    client_environment = _load_llm_environment(client_environment, data_root)
    _configure_memory_environment(backend_environment, data_root)
    if not any(backend_environment.get(name) for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")):
        print("LLM_API_KEY_NOT_CONFIGURED: 请先在启动此程序的进程环境中设置 API key；当前仅提供明确的 safe-static/degraded 回退。")
    server = None
    try:
        if health != "READY":
            server, health = _start_backend_server(
                backend=backend,
                entrypoint=entrypoint,
                environment=backend_environment,
                port=args.port,
                data_root=data_root,
                expected_backend_id=expected_backend_id,
            )
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
        try:
            _repair_client_frontend(root, args.port)
        except (CompanionSettingsPatchError, OSError):
            print("CLIENT_FRONTEND_REPAIR_FAILED")
            return 2
        owned_ready = (
            server is not None
            and server.poll() is None
            and _health(args.port) == "READY"
            and _server_backend_id(args.port) == expected_backend_id
        )
        if not owned_ready:
            if server is not None:
                _stop_backend_server(server)
                server = None
            health = _health(args.port)
            if health == "PORT_CONFLICT":
                print("PORT_CONFLICT")
                return 2
            if health == "READY":
                if not _stop_stale_backend(args.port, root):
                    print("STALE_BACKEND_RUNNING")
                    return 2
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    health = _health(args.port)
                    if health == "UNAVAILABLE":
                        break
                    time.sleep(0.1)
                if health != "UNAVAILABLE":
                    print("STALE_BACKEND_RUNNING")
                    return 2
            server, health = _start_backend_server(
                backend=backend,
                entrypoint=entrypoint,
                environment=backend_environment,
                port=args.port,
                data_root=data_root,
                expected_backend_id=expected_backend_id,
            )
            if health != "READY":
                print("LOCAL_SERVER_UNAVAILABLE")
                return 2
        profile = root / "profile"
        roaming = profile / "Roaming"
        local = profile / "Local"
        roaming.mkdir(parents=True, exist_ok=True)
        local.mkdir(parents=True, exist_ok=True)
        return subprocess.call(
            _client_command(client, local),
            cwd=root / "app",
            env=_client_environment(client_environment, roaming, local),
        )
    finally:
        # Stop only the backend process spawned and owned by this launcher.
        # A healthy backend reused from an earlier launcher has no local handle.
        if server is not None:
            _stop_backend_server(server)


if __name__ == "__main__":
    raise SystemExit(main())
