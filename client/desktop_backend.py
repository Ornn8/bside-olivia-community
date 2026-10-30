"""Run the existing Olivia backend for the macOS Electron app."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import signal
import shutil
import sys

from aiohttp import ClientSession
from aiohttp import web


DESKTOP_TOKEN_HEADER = "X-Olivia-Desktop-Token"
DESKTOP_HANDSHAKE_SCHEMA = "olivia-ready.v1"
# Electron maps this exit status to BACKEND_ALREADY_RUNNING.
DESKTOP_LOCKED_EXIT_CODE = 75


class DesktopBackendLocked(RuntimeError):
    """Another backend process already owns this data root."""


def acquire_data_root_lock(root: Path):
    """Hold an exclusive lock on the data root for this process's lifetime.

    The kernel releases the lock when the process exits, so a crashed backend
    never blocks a restart, while two live backends can never share one root.
    """

    import fcntl  # The Electron backend only runs on macOS.

    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "desktop-backend.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise DesktopBackendLocked("DESKTOP_BACKEND_ALREADY_RUNNING") from None
    return handle


def watch_parent_pipe(loop: asyncio.AbstractEventLoop, fd: int, stopped: asyncio.Event) -> None:
    """Stop when Electron's end of the stdin pipe closes, even after SIGKILL."""

    def readable() -> None:
        try:
            data = os.read(fd, 4096)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(fd)
            stopped.set()

    loop.add_reader(fd, readable)


def desktop_ready_message(*, port: int, pid: int, nonce: str) -> dict[str, object]:
    """Build the private Electron/backend startup handshake."""

    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("DESKTOP_PORT_INVALID")
    if not isinstance(pid, int) or pid < 1:
        raise ValueError("DESKTOP_PID_INVALID")
    if not isinstance(nonce, str) or not nonce:
        raise ValueError("DESKTOP_NONCE_INVALID")
    return {
        "schema_version": DESKTOP_HANDSHAKE_SCHEMA,
        "kind": "olivia-ready",
        "port": port,
        "pid": pid,
        "nonce": nonce,
    }


def desktop_update_status() -> dict[str, str | None]:
    """Keep the Windows local-update response shape on the Mac backend."""

    from original_client_update_api import running_component_version

    version = running_component_version()
    if version.get("version") is None:
        version["version"] = os.environ.get("OLIVIA_DESKTOP_VERSION", "2.5.7")
    return version


def install_desktop_auth(app: web.Application, token: str | None) -> None:
    """Require the Electron-only bearer token on every backend request."""
    if not token:
        return

    @web.middleware
    async def desktop_auth(request: web.Request, handler):
        supplied = request.headers.get(DESKTOP_TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied, token):
            return web.json_response(
                {"status": "FAILED", "error_code": "DESKTOP_AUTH_REQUIRED"},
                status=401,
            )
        return await handler(request)

    app.middlewares.insert(0, desktop_auth)


def install_desktop_status_routes(app: web.Application, *, model_ready: bool) -> None:
    """Answer original-client status calls for capabilities bundled in the Mac app."""
    from original_client_setup_api import _authorize, _headers

    @web.middleware
    async def desktop_status(request: web.Request, handler):
        if request.path not in {"/toy/capabilities/mem0", "/toy/updates/local/status"}:
            return await handler(request)
        origin = _authorize(request, confirm=False)
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=_headers(origin, preflight=True))
        if request.method != "GET":
            return web.Response(status=405, headers=_headers(origin))
        if request.path == "/toy/updates/local/status":
            return web.json_response({"status": "READY", **desktop_update_status()}, headers=_headers(origin))
        return web.json_response({
            "schema_version": "olivia.capability-status.v2",
            "status": "READY" if model_ready else "UNAVAILABLE",
            "capability": "long_term_memory",
            "state": "ready" if model_ready else "repair",
            "phase": "ready" if model_ready else "verification",
            "downloaded_bytes": 0, "total_bytes": 0, "remaining_bytes": 0,
            "installed_bytes": 0, "install_locations": [
                {"root": "local_data_root", "relative_path": "memory/model-cache"},
            ],
            "version": "bundled", "license_summary": "MIT", "requires_gpu": False,
            **({} if model_ready else {"reason_code": "MEMORY_MODEL_UNAVAILABLE"}),
        }, headers=_headers(origin))

    app.middlewares.insert(0, desktop_status)


def install_desktop_websocket(app: web.Application) -> None:
    """Provide the original client's long-lived local push channel.

    Electron forwards the browser upgrade to this route.  Keeping the socket
    alive lets the original client establish its normal realtime transport;
    incoming application messages are intentionally ignored because all
    state-changing operations still go through the authenticated HTTP API.
    aiohttp handles ping/pong frames and disconnect cleanup for us.
    """

    async def websocket(request: web.Request) -> web.StreamResponse:
        socket = web.WebSocketResponse(heartbeat=30, autoping=True)
        await socket.prepare(request)
        async for message in socket:
            if message.type == web.WSMsgType.ERROR:
                break
            if message.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED):
                break
        return socket

    app.router.add_get("/ws", websocket)


async def serve() -> None:
    root = Path(os.environ["OLIVIA_LOCAL_DATA_ROOT"])
    if not root.is_absolute():
        raise ValueError("OLIVIA_LOCAL_DATA_ROOT must be absolute")
    root_lock = acquire_data_root_lock(root)  # noqa: F841 - held until exit

    # Match the installed client runtime, but keep every writable path in this
    # app's own Application Support directory.
    from installer.start_local import _configure_memory_environment, _load_llm_environment
    from original_client_relay_api import RELAY_BASE

    environment = _load_llm_environment(dict(os.environ), root, include_secret=True)
    environment.setdefault("OLIVIA_JEV_DECISION_URL", RELAY_BASE + "/companion/decide")
    environment.setdefault("OLIVIA_JEV_BILLING_ENABLED", "1")
    environment.setdefault("OLIVIA_PRIVATE_WORLD_ENABLED", "1")
    environment.setdefault("OLIVIA_PRIVATE_WORLD_DB", str(root / "private_world" / "private_world.sqlite3"))
    environment.setdefault("OLIVIA_PROVIDER_CACHE_ROOT", str(root / "provider-cache"))
    environment.setdefault("OLIVIA_REPLY_DELAY_ENABLED", "0")
    environment.setdefault("OLIVIA_MEMORY_LLM_DEFAULT_BASE_URL", environment.get("OLIVIA_LLM_BASE_URL", ""))
    environment.setdefault("OLIVIA_MEMORY_LLM_DEFAULT_MODEL", environment.get("OLIVIA_LLM_MODEL", ""))
    environment.setdefault("OLIVIA_MEMORY_LLM_DEFAULT_API_KEY_ENV", "OLIVIA_LLM_API_KEY")
    environment["OLIVIA_DESKTOP_PROACTIVE_OPT_IN"] = "1"
    environment["OLIVIA_DESKTOP_MEDIA_OPT_IN"] = "1"
    environment["OLIVIA_DESKTOP_DYNAMIC_PORT"] = "1"
    _configure_memory_environment(environment, root)
    import imageio_ffmpeg
    environment["OLIVIA_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
    bundled_model = environment.get("OLIVIA_DESKTOP_MODEL_CACHE")
    if bundled_model:
        from mem0_memory import load_mem0_config, verified_embedding_cache

        installed = load_mem0_config(environ=environment)
        if not verified_embedding_cache(installed):
            source = Path(bundled_model)
            source_config = load_mem0_config(environ={
                **environment, "OLIVIA_MEMORY_EMBEDDING_CACHE": str(source),
            })
            if not verified_embedding_cache(source_config):
                raise RuntimeError("BUNDLED_MEMORY_MODEL_INVALID")
            shutil.copytree(source, installed.model_cache, dirs_exist_ok=True)
            if not verified_embedding_cache(installed):
                raise RuntimeError("INSTALLED_MEMORY_MODEL_INVALID")
    os.environ.update(environment)

    import local_server
    from original_client_server import create_configured_original_client_server_runtime
    from mem0_memory import load_mem0_config, verified_embedding_cache

    local_server.recover_pending_private_world()
    runtime = create_configured_original_client_server_runtime(server_module=local_server)
    install_desktop_status_routes(runtime.app, model_ready=verified_embedding_cache(load_mem0_config()))
    install_desktop_websocket(runtime.app)
    install_desktop_auth(runtime.app, os.environ.get("OLIVIA_DESKTOP_REQUEST_TOKEN"))
    runner = web.AppRunner(runtime.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    try:
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        os.environ["OLIVIA_DESKTOP_BOUND_PORT"] = str(port)
        local_server.PORT = port
        async with ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/health?probe=startup",
                headers={DESKTOP_TOKEN_HEADER: os.environ.get("OLIVIA_DESKTOP_REQUEST_TOKEN", "")},
            ) as response:
                health = await response.json()
                if response.status != 200 or health.get("code") != 0:
                    raise RuntimeError("BACKEND_HEALTH_FAILED")
        print(json.dumps(desktop_ready_message(
            port=port, pid=os.getpid(), nonce=os.environ.get("OLIVIA_DESKTOP_START_NONCE", "")
        )), flush=True)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stopped.set)
        if os.environ.get("OLIVIA_DESKTOP_PARENT_PIPE") == "1":
            watch_parent_pipe(loop, sys.stdin.fileno(), stopped)
        await stopped.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except DesktopBackendLocked:
        sys.exit(DESKTOP_LOCKED_EXIT_CODE)
