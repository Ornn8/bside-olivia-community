"""Start the local HTTP server and the isolated copied client."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
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
            input=protected + "\n", text=True, capture_output=True, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, UnicodeError):
        return ""


def _health(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health?profile=core", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return response.status == 200 and isinstance(payload, dict)
    except Exception:
        return False


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=int(os.environ.get("OLIVIA_PORT", "8899")))
    parser.add_argument("--health-only", action="store_true")
    args = parser.parse_args(argv)
    root = args.install_root.expanduser().resolve()
    backend = root / "local_backend"
    if not (backend / "local_server.py").is_file():
        print("PATCH_PAYLOAD_INCOMPLETE")
        return 2
    if args.health_only:
        ready = _health(args.port)
        print(json.dumps({"status": "READY" if ready else "UNAVAILABLE"}))
        return 0 if ready else 2
    data_root = root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    configured_key = _load_dpapi_key(data_root / "config" / "deepseek_api_key.dpapi")
    if configured_key and not any(environment.get(name) for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")):
        environment["DEEPSEEK_API_KEY"] = configured_key
    environment.update(
        {
            "OLIVIA_INSTALL_ROOT": str(root),
            "OLIVIA_PROJECT_ROOT": str(root / "app"),
            "OLIVIA_LOCAL_DATA_ROOT": str(data_root),
            "OLIVIA_MEMORY_ROOT": str(data_root / "memory"),
            "OLIVIA_REPLY_DELAY_ENABLED": "1",
            "OLIVIA_REPLY_DELAY_MINUTES_MIN": "5",
            "OLIVIA_REPLY_DELAY_MINUTES_MAX": "10",
            "OLIVIA_LLM_PROVIDER": "openai_compatible",
            "OLIVIA_LLM_BASE_URL": "https://api.deepseek.com",
            "OLIVIA_LLM_MODEL": "deepseek-v4-flash",
            "OLIVIA_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
            "OLIVIA_LLM_API_STYLE": "chat_completions",
            "OLIVIA_LLM_STREAM": "true",
            "OLIVIA_LLM_TIMEOUT_SECONDS": "25",
            "OLIVIA_LLM_MAX_RETRIES": "0",
            "OLIVIA_PORT": str(args.port),
        }
    )
    if not any(environment.get(name) for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")):
        print("LLM_API_KEY_NOT_CONFIGURED: 请先在启动此程序的进程环境中设置 API key；当前仅提供明确的 safe-static/degraded 回退。")
    server = None
    if not _health(args.port):
        detached = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        server = subprocess.Popen([str(_backend_executable()), str(backend / "local_server.py")], cwd=backend, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=detached)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and server.poll() is None:
            if _health(args.port):
                break
            time.sleep(0.25)
        if not _health(args.port):
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
    return subprocess.call(_client_command(client, local), cwd=root / "app", env=_client_environment(environment, roaming, local))


if __name__ == "__main__":
    raise SystemExit(main())
