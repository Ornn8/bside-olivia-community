"""Local, user-confirmed QQ/Weixin binding for personal chat.

The setup surface keeps account credentials on this Windows installation and
only exposes content-free status plus short-lived setup presentation state.
"""
from __future__ import annotations

import base64
import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from .probe import checked_url, wechat_request
from .qr_svg import QRPayloadTooLong, svg_bytes


STATUS_PATH = "/toy/personal-chat/setup/status"
CHANNEL_CHOICE_PATH = "/toy/personal-chat/setup/channel-choice"
WECHAT_START_PATH = "/toy/personal-chat/setup/wechat/start"
WECHAT_VERIFY_PATH = "/toy/personal-chat/setup/wechat/verify"
QQ_CONFIGURE_PATH = "/toy/personal-chat/setup/qq/configure"
NAPCAT_INSTALL_PATH = "/toy/personal-chat/setup/qq/napcat/install"
NAPCAT_START_PATH = "/toy/personal-chat/setup/qq/napcat/start"
NAPCAT_LOGIN_PATH = "/toy/personal-chat/setup/qq/napcat/login"
NAPCAT_BROWSER_PATH = "/toy/personal-chat/setup/qq/napcat/browser"
CONFIRM_HEADER = "X-Olivia-Companion-Action"
CONFIRM_VALUE = "confirmed"
_SETUP = web.AppKey("personal_chat_setup", dict)
_QQ_ID = re.compile(r"^[1-9][0-9]{4,19}$")
_VERIFY_CODE = re.compile(r"^[0-9]{4,8}$")
_SETUP_PATHS = {
    STATUS_PATH: frozenset({"GET"}),
    CHANNEL_CHOICE_PATH: frozenset({"POST"}),
    WECHAT_START_PATH: frozenset({"POST"}),
    WECHAT_VERIFY_PATH: frozenset({"POST"}),
    QQ_CONFIGURE_PATH: frozenset({"POST"}),
    NAPCAT_INSTALL_PATH: frozenset({"POST"}),
    NAPCAT_START_PATH: frozenset({"POST"}),
    NAPCAT_LOGIN_PATH: frozenset({"GET", "POST"}),
    NAPCAT_BROWSER_PATH: frozenset({"POST"}),
}
_NAPCAT_WATCHDOG_SECONDS = 15.0


def _failure_code(exc: BaseException) -> str:
    code = str(exc)
    if re.fullmatch(r"(?:PERSONAL_CHAT|WECHAT|QQ|NAPCAT)_[A-Z0-9_]{1,80}", code):
        return code
    return "PERSONAL_CHAT_SETUP_UNAVAILABLE"


def _origin_allowed(server, origin: str) -> bool:
    if not origin:
        return True
    checker = getattr(server, "origin_allowed", None)
    if callable(checker):
        try:
            return bool(checker(origin))
        except Exception:
            return False
    trusted = getattr(server, "TRUSTED_FRONTEND_ORIGINS", ())
    return origin in trusted


def _cors_headers(request: web.Request, server, *, preflight: bool = False) -> dict[str, str] | None:
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    origin = request.headers.get("Origin", "")
    if origin:
        if not _origin_allowed(server, origin):
            return None
        headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
    if preflight:
        headers.update(
            {
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": f"Content-Type, {CONFIRM_HEADER}",
                "Access-Control-Max-Age": "600",
            }
        )
    return headers


def _root(server) -> Path:
    value = server._state_root()
    if value is None:
        raise RuntimeError("PERSONAL_CHAT_DURABLE_STATE_REQUIRED")
    root = Path(value)
    if not root.is_absolute():
        raise RuntimeError("PERSONAL_CHAT_DURABLE_STATE_REQUIRED")
    return root


def _config_path(server) -> Path:
    return _root(server) / "personal-chat" / "config.json"


def _read_config(server) -> dict[str, object]:
    path = _config_path(server)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("PERSONAL_CHAT_CONFIG_INVALID") from exc
    if not isinstance(value, dict) or set(value) - {"wechat", "qq"}:
        raise RuntimeError("PERSONAL_CHAT_CONFIG_INVALID")
    return value


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_config(server, value: dict[str, object]) -> None:
    if not value or set(value) - {"wechat", "qq"}:
        raise RuntimeError("PERSONAL_CHAT_CONFIG_INVALID")
    _atomic_text(
        _config_path(server),
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _selected_channels(server) -> set[str]:
    from .backend import selected_channels

    return selected_channels(server)


def _contact_access(server) -> dict[str, object]:
    from .contact_invitation import status

    port = getattr(server, "private_world_port", None)
    snapshot = port.snapshot() if port is not None else None
    value = status(server.store.letters, snapshot)
    return dict(value) if isinstance(value, dict) else {"state": "locked", "channels": []}


def _store_setup_choice(server, choice: str) -> list[str]:
    if choice not in {"qq", "wechat", "both"}:
        raise RuntimeError("PERSONAL_CHAT_CHANNEL_CHOICE_INVALID")
    access = _contact_access(server)
    invitation_id = access.get("invitation_id")
    if access.get("state") not in {"invited", "qq", "wechat", "both"} or not isinstance(invitation_id, str):
        raise RuntimeError("PERSONAL_CHAT_INVITATION_REQUIRED")
    invitation = next(
        (
            row
            for row in server.store.letters
            if row.get("letter_id") == invitation_id
            and row.get("origin") == "proactive"
            and row.get("proactive_kind") == "contact_invitation"
            and row.get("letter_status") == "COMPLETED"
        ),
        None,
    )
    if invitation is None:
        raise RuntimeError("PERSONAL_CHAT_INVITATION_REQUIRED")
    persist = getattr(server, "_persist_store_state", None)
    if not callable(persist):
        raise RuntimeError("PERSONAL_CHAT_DURABLE_STATE_REQUIRED")
    channels = set(access.get('channels', [])) | ({'qq', 'wechat'} if choice == 'both' else {choice})
    invitation["contact_setup_choice"] = 'both' if len(channels) == 2 else choice
    invitation["contact_setup_choice_at"] = time.time()
    persist()
    return sorted(channels)


def _qr_content(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise RuntimeError("WECHAT_QR_UNAVAILABLE")
    if "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise RuntimeError("WECHAT_QR_UNAVAILABLE") from exc
        hostname = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not hostname
            or (hostname != "weixin.qq.com" and not hostname.endswith(".weixin.qq.com"))
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise RuntimeError("WECHAT_QR_UNAVAILABLE")
    return value


def _qr_data_url(value: object) -> str:
    content = _qr_content(value)
    try:
        image = svg_bytes(content)
    except (QRPayloadTooLong, ValueError) as exc:
        raise RuntimeError("WECHAT_QR_UNAVAILABLE") from exc
    encoded = base64.b64encode(image).decode("ascii")
    return "data:image/svg+xml;base64," + encoded


def _public_status(request: web.Request, server) -> dict[str, object]:
    config = _read_config(server)
    selected = _selected_channels(server)
    configured = {name: name in config for name in ("wechat", "qq")}
    from . import backend, napcat_installer

    active = request.app.get(backend._RUNTIME)
    listener = dict(active.get("status", {})) if isinstance(active, dict) else {}
    last_seen_at = dict(active.get("last_seen_at", {})) if isinstance(active, dict) else {}
    delivery_health = dict(active.get("delivery_health", {})) if isinstance(active, dict) else {}
    e2e_verified_at = dict(active.get("e2e_verified_at", {})) if isinstance(active, dict) else {}
    pending_tests = set(active.get("connection_tests", {}).keys()) if isinstance(active, dict) else set()
    runtime = request.app[_SETUP]
    wechat = dict(runtime.get("wechat", {"state": "IDLE"}))
    wechat.pop("qrcode", None)
    wechat.pop("verify_code", None)
    qq = dict(runtime.get("qq", {"state": "IDLE"}))
    return {
        "contact_state": str(_contact_access(server).get("state", "locked")),
        "selected_channels": sorted(selected),
        "configured": configured,
        "listeners": {
            name: listener.get(
                name,
                "CONFIGURED_RESTART" if configured[name] else "SETUP_REQUIRED",
            )
            for name in selected
        },
        "last_seen_at": {name: last_seen_at.get(name) for name in selected if last_seen_at.get(name)},
        "delivery_health": {name: delivery_health.get(name) for name in selected if delivery_health.get(name)},
        "e2e_verified_at": {name: e2e_verified_at.get(name) for name in selected if e2e_verified_at.get(name)},
        "connection_test_pending": sorted(name for name in selected if name in pending_tests),
        "wechat": wechat,
        "qq": qq,
        "napcat": napcat_installer.public_status(_root(server), runtime),
    }


async def _wechat_login(server, runtime: dict[str, object]) -> None:
    state = runtime["wechat"]
    assert isinstance(state, dict)
    base = "https://ilinkai.weixin.qq.com"
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            qr = await wechat_request(
                session,
                base,
                "/ilink/bot/get_bot_qrcode",
                params={"bot_type": "3"},
                body={"local_token_list": []},
            )
            qrcode_id = qr.get("qrcode")
            if not isinstance(qrcode_id, str) or not qrcode_id:
                raise RuntimeError("WECHAT_QR_UNAVAILABLE")
            state.clear()
            state.update(
                state="SCAN_REQUIRED",
                qr_data=_qr_data_url(qr.get("qrcode_img_content")),
                qrcode=qrcode_id,
            )
            for _ in range(150):
                await asyncio.sleep(2)
                params = {"qrcode": qrcode_id}
                verify_code = state.get("verify_code")
                if verify_code:
                    params["verify_code"] = verify_code
                login = await wechat_request(
                    session, base, "/ilink/bot/get_qrcode_status", params=params
                )
                current = login.get("status")
                if current == "confirmed":
                    next_base = checked_url(login.get("baseurl") or base)
                    credentials = {
                        "token": login["bot_token"],
                        "account": login["ilink_bot_id"],
                        "owner": login["ilink_user_id"],
                        "base": next_base,
                    }
                    from original_client_setup_api import _dpapi_protect

                    secret = _root(server) / "personal-chat" / "wechat.dpapi"
                    _atomic_text(
                        secret,
                        _dpapi_protect(json.dumps(credentials, ensure_ascii=False)),
                    )
                    config = _read_config(server)
                    config["wechat"] = {"credentials_file": str(secret)}
                    _write_config(server, config)
                    state.clear()
                    state.update(state="READY_RESTART")
                    return
                if current == "scaned":
                    state["state"] = "SCANNED"
                    continue
                if current == "need_verifycode":
                    state["state"] = "VERIFY_REQUIRED"
                    state.pop("verify_code", None)
                    continue
                if current == "scaned_but_redirect":
                    host = login.get("redirect_host", "")
                    base = checked_url(
                        host if str(host).startswith("https://") else "https://" + str(host)
                    )
                    state["state"] = "SCANNED"
                    continue
                if current in {"wait", None}:
                    state["state"] = "SCAN_REQUIRED"
                    continue
                if current in {"expired", "verify_code_blocked", "binded_redirect"}:
                    raise RuntimeError("WECHAT_LOGIN_REQUIRES_ATTENTION")
                raise RuntimeError("WECHAT_LOGIN_REQUIRES_ATTENTION")
            raise RuntimeError("WECHAT_LOGIN_EXPIRED")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state.clear()
        state.update(state="FAILED", error=_failure_code(exc))


async def _qq_probe(url: str, token: str, expected_account: str | None = None) -> str:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(
            checked_url(url, local=True),
            headers={"Authorization": "Bearer " + token},
            heartbeat=20,
        ) as ws:
            await ws.send_json({"action": "get_login_info", "echo": "olivia-setup"})
            for _ in range(20):
                message = await asyncio.wait_for(ws.receive(), 10)
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                raw = json.loads(message.data)
                if raw.get("echo") != "olivia-setup":
                    continue
                if (
                    raw.get("status") != "ok"
                    or raw.get("retcode") != 0
                    or not isinstance(raw.get("data"), dict)
                ):
                    raise RuntimeError("QQ_LOGIN_UNAVAILABLE")
                account = str(raw["data"].get("user_id", ""))
                if not _QQ_ID.fullmatch(account):
                    raise RuntimeError("QQ_LOGIN_UNAVAILABLE")
                if expected_account is not None and account != expected_account:
                    raise RuntimeError("QQ_ACCOUNT_MISMATCH")
                return account
    raise RuntimeError("QQ_LOGIN_UNAVAILABLE")


async def _prepare_napcat(server, runtime: dict[str, object]) -> None:
    from . import napcat_installer

    runtime["napcat_state"] = "DOWNLOADING"
    try:
        await asyncio.to_thread(napcat_installer.install_component, _root(server))
        runtime["napcat_state"] = "READY"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        runtime["napcat_state"] = "FAILED"
        runtime["napcat_error"] = _failure_code(exc)


async def _open_napcat_login(server, runtime: dict[str, object]) -> None:
    from . import napcat_installer

    for _ in range(60):
        await asyncio.sleep(1)
        if await asyncio.to_thread(napcat_installer.webui_available, _root(server)):
            runtime.pop('napcat_error', None)
            if runtime.get('napcat_state') != 'ONEBOT_READY':
                runtime['napcat_state'] = 'AWAITING_QQ_LOGIN'
            return
        process = runtime.get('napcat_shell_process')
        if process is not None and process.poll() is not None:
            runtime['napcat_state'] = 'FAILED'
            runtime['napcat_error'] = 'NAPCAT_START_FAILED'
            return
    if runtime.get("napcat_state") == "ONEBOT_READY":
        return
    ready = await asyncio.to_thread(napcat_installer.webui_available, _root(server))
    runtime["napcat_state"] = "AWAITING_QQ_LOGIN" if ready else "FAILED"
    runtime["napcat_error"] = "NAPCAT_LOGIN_OPEN_FAILED" if ready else "NAPCAT_START_TIMEOUT"


def install_setup_routes(app: web.Application, server) -> None:
    if _SETUP in app:
        return
    runtime: dict[str, object] = {
        "wechat": {"state": "IDLE"},
        "qq": {"state": "IDLE"},
        "wechat_task": None,
        "napcat_state": "IDLE",
        "napcat_task": None,
        "napcat_login_task": None,
        "napcat_watchdog_task": None,
        "napcat_shell_process": None,
        "napcat_account": None,
    }
    app[_SETUP] = runtime
    napcat_start_lock = asyncio.Lock()

    async def refresh_managed_napcat_state() -> str:
        from . import napcat_installer

        root = _root(server)
        if await asyncio.to_thread(napcat_installer.onebot_available):
            try:
                url, token = await asyncio.to_thread(napcat_installer.managed_connection, root)
                account = await asyncio.wait_for(_qq_probe(url, token), 2)
            except Exception:
                runtime["napcat_state"] = "ONEBOT_PROBING"
                return "ONEBOT_PROBING"
            runtime["napcat_account"] = account
            ready = await asyncio.to_thread(napcat_installer.account_config_ready, root, account)
            runtime["napcat_state"] = "ONEBOT_READY" if ready else "ONEBOT_CONFIG_PENDING"
            if ready:
                runtime.pop("napcat_error", None)
            return str(runtime["napcat_state"])
        runtime["napcat_account"] = None
        if await asyncio.to_thread(napcat_installer.webui_available, root):
            runtime["napcat_state"] = "AWAITING_QQ_LOGIN"
            return "AWAITING_QQ_LOGIN"
        if runtime.get("napcat_state") == "FAILED":
            return "FAILED"
        state = "READY" if runtime.get("napcat_state") in {"IDLE", "READY"} else "STARTING"
        runtime["napcat_state"] = state
        return state

    async def napcat_watchdog() -> None:
        from . import napcat_installer

        while True:
            try:
                await asyncio.sleep(_NAPCAT_WATCHDOG_SECONDS)
                config = _read_config(server)
                qq = config.get("qq")
                if not isinstance(qq, dict) or qq.get("managed") is not True:
                    return
                process = runtime.get("napcat_shell_process")
                alive = process is not None and getattr(process, "poll", lambda: 0)() is None
                state = await refresh_managed_napcat_state()
                if state != "STARTING":
                    continue
                if alive:
                    continue
                process = await asyncio.to_thread(napcat_installer.ensure_shell, _root(server))
                runtime["napcat_shell_process"] = process
                await refresh_managed_napcat_state()
                runtime.pop("napcat_error", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime["napcat_state"] = "FAILED"
                runtime["napcat_error"] = _failure_code(exc)

    async def start_managed_napcat(_application: web.Application) -> None:
        try:
            config = _read_config(server)
            qq = config.get("qq")
            if not isinstance(qq, dict) or qq.get("managed") is not True:
                return
            from . import napcat_installer

            runtime["napcat_state"] = "STARTING"
            process = await asyncio.to_thread(napcat_installer.ensure_shell, _root(server))
            runtime["napcat_shell_process"] = process
            await refresh_managed_napcat_state()
            task = runtime.get("napcat_watchdog_task")
            if not isinstance(task, asyncio.Task) or task.done():
                runtime["napcat_watchdog_task"] = asyncio.create_task(napcat_watchdog())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime["napcat_state"] = "FAILED"
            runtime["napcat_error"] = _failure_code(exc)

    async def status(request: web.Request) -> web.Response:
        try:
            if "qq" in _selected_channels(server):
                from . import napcat_installer
                if napcat_installer.find_shell(_root(server)) is not None and runtime.get("napcat_state") != "DOWNLOADING":
                    await refresh_managed_napcat_state()
            return web.json_response(_public_status(request, server))
        except Exception as exc:
            return web.json_response({"error": _failure_code(exc)}, status=503)

    async def channel_choice(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "PERSONAL_CHAT_CHANNEL_CHOICE_INVALID"}, status=400)
        choice = body.get("choice") if isinstance(body, dict) else None
        try:
            channels = _store_setup_choice(server, str(choice or ""))
            return web.json_response({"status": "SELECTED", "channels": channels})
        except Exception as exc:
            code = _failure_code(exc)
            return web.json_response(
                {"error": code},
                status=409 if code == "PERSONAL_CHAT_INVITATION_REQUIRED" else 400,
            )

    async def wechat_start(request: web.Request) -> web.Response:
        try:
            if "wechat" not in _selected_channels(server):
                return web.json_response({"error": "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"}, status=409)
            task = runtime.get("wechat_task")
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            runtime["wechat"] = {"state": "STARTING"}
            task = asyncio.create_task(_wechat_login(server, runtime))
            runtime["wechat_task"] = task
            return web.json_response({"status": "STARTING"}, status=202)
        except Exception as exc:
            return web.json_response({"error": _failure_code(exc)}, status=503)

    async def wechat_verify(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "WECHAT_VERIFY_CODE_INVALID"}, status=400)
        code = body.get("code") if isinstance(body, dict) else None
        state = runtime.get("wechat")
        if (
            not isinstance(state, dict)
            or state.get("state") != "VERIFY_REQUIRED"
            or not isinstance(code, str)
            or not _VERIFY_CODE.fullmatch(code)
        ):
            return web.json_response({"error": "WECHAT_VERIFY_CODE_INVALID"}, status=400)
        state["verify_code"] = code
        state["state"] = "SCANNED"
        return web.json_response({"status": "VERIFYING"})

    async def napcat_install(request: web.Request) -> web.Response:
        if "qq" not in _selected_channels(server):
            return web.json_response({"error": "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"}, status=409)
        task = runtime.get("napcat_task")
        if isinstance(task, asyncio.Task) and not task.done():
            return web.json_response(
                {"status": str(runtime.get("napcat_state") or "DOWNLOADING")},
                status=202,
            )
        runtime.pop("napcat_error", None)
        runtime["napcat_state"] = "DOWNLOADING"
        task = asyncio.create_task(_prepare_napcat(server, runtime))
        runtime["napcat_task"] = task
        return web.json_response({"status": "DOWNLOADING"}, status=202)

    async def napcat_start(request: web.Request) -> web.Response:
        async with napcat_start_lock:
            return await start_napcat_once(request)

    async def start_napcat_once(request: web.Request) -> web.Response:
        if "qq" not in _selected_channels(server):
            return web.json_response({"error": "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"}, status=409)
        from . import napcat_installer

        try:
            login_task = runtime.get("napcat_login_task")
            if isinstance(login_task, asyncio.Task) and not login_task.done():
                return web.json_response({"status": str(runtime.get("napcat_state") or "STARTING")}, status=202)
            runtime.pop("napcat_error", None)
            runtime["napcat_state"] = "STARTING"
            process = runtime.get("napcat_shell_process")
            if process is None or getattr(process, "poll", lambda: 0)() is not None:
                process = await asyncio.to_thread(napcat_installer.ensure_shell, _root(server))
            runtime["napcat_shell_process"] = process
            await refresh_managed_napcat_state()
            login_task = runtime.get("napcat_login_task")
            if not isinstance(login_task, asyncio.Task) or login_task.done():
                login_task = asyncio.create_task(_open_napcat_login(server, runtime))
                runtime["napcat_login_task"] = login_task
            return web.json_response({"status": str(runtime.get("napcat_state") or "STARTING")}, status=202)
        except Exception as exc:
            code = _failure_code(exc)
            runtime["napcat_state"] = "FAILED"
            runtime["napcat_error"] = code
            return web.json_response({"error": code}, status=400)

    async def napcat_login(request: web.Request) -> web.Response:
        if "qq" not in _selected_channels(server):
            return web.json_response({"error": "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"}, status=409)
        from . import napcat_installer
        data = await asyncio.to_thread(napcat_installer.login_status, _root(server),
                                       refresh=request.method == "POST")
        result = {key: data[key] for key in ('logged_in', 'scanned', 'verification_required')}
        if not result['logged_in'] and not result['scanned']:
            qr = data.get('qr')
            if isinstance(qr, str) and 0 < len(qr) <= 4096:
                try:
                    result['qr_data'] = 'data:image/svg+xml;base64,' + base64.b64encode(svg_bytes(qr)).decode('ascii')
                except (QRPayloadTooLong, ValueError):
                    pass
        return web.json_response(result)

    async def napcat_browser(request: web.Request) -> web.Response:
        if "qq" not in _selected_channels(server):
            return web.json_response({"error": "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"}, status=409)
        from . import napcat_installer
        if not await asyncio.to_thread(napcat_installer.open_login_page, _root(server)):
            return web.json_response({"error": "NAPCAT_LOGIN_OPEN_FAILED"}, status=503)
        return web.json_response({"status": "OPEN_REQUESTED"})

    async def qq_configure(request: web.Request) -> web.Response:
        if "qq" not in _selected_channels(server):
            return web.json_response({"error": "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"}, status=409)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "QQ_SETUP_INVALID"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "QQ_SETUP_INVALID"}, status=400)
        managed = body.get("managed") is True
        owner = str(body.get("owner", "")).strip()
        try:
            if not _QQ_ID.fullmatch(owner):
                raise RuntimeError("QQ_SETUP_INVALID")
            if managed:
                from . import napcat_installer

                if runtime.get("napcat_state") != "ONEBOT_READY":
                    raise RuntimeError("QQ_LOGIN_UNAVAILABLE")
                expected = str(runtime.get("napcat_account") or "")
                if not _QQ_ID.fullmatch(expected):
                    raise RuntimeError("QQ_LOGIN_UNAVAILABLE")
                url, token = await asyncio.to_thread(
                    napcat_installer.managed_connection, _root(server)
                )
                account = await _qq_probe(url, token, expected)
                if not await asyncio.to_thread(
                    napcat_installer.account_config_ready, _root(server), account
                ):
                    raise RuntimeError("NAPCAT_ONEBOT_CONFIG_PENDING")
            else:
                account = str(body.get("account", "")).strip()
                token = str(body.get("token", ""))
                url = str(body.get("url", "ws://127.0.0.1:3001")).strip()
                parsed = urlsplit(checked_url(url, local=True))
                if parsed.scheme != "ws" or not _QQ_ID.fullmatch(account):
                    raise RuntimeError("QQ_SETUP_INVALID")
                if not 16 <= len(token) <= 512:
                    raise RuntimeError("QQ_TOKEN_INVALID")
                await _qq_probe(url, token, account)
            if account == owner:
                raise RuntimeError("QQ_BOT_AND_OWNER_MUST_DIFFER")
            runtime["qq"] = {"state": "TESTING"}
            from original_client_setup_api import _dpapi_protect

            secret = _root(server) / "personal-chat" / "qq.dpapi"
            _atomic_text(
                secret,
                _dpapi_protect(json.dumps({"token": token}, ensure_ascii=False)),
            )
            config = _read_config(server)
            config["qq"] = {
                "url": url,
                "account": account,
                "owner": owner,
                "credentials_file": str(secret),
                "managed": managed,
            }
            _write_config(server, config)
            runtime["qq"] = {"state": "READY_RESTART"}
            return web.json_response({"status": "READY_RESTART"})
        except Exception as exc:
            code = _failure_code(exc)
            runtime["qq"] = {"state": "FAILED", "error": code}
            return web.json_response(
                {"error": code},
                status=400 if code.startswith(("QQ_", "NAPCAT_")) else 503,
            )

    endpoints = {
        STATUS_PATH: status,
        CHANNEL_CHOICE_PATH: channel_choice,
        WECHAT_START_PATH: wechat_start,
        WECHAT_VERIFY_PATH: wechat_verify,
        QQ_CONFIGURE_PATH: qq_configure,
        NAPCAT_INSTALL_PATH: napcat_install,
        NAPCAT_START_PATH: napcat_start,
        NAPCAT_LOGIN_PATH: napcat_login,
        NAPCAT_BROWSER_PATH: napcat_browser,
    }

    @web.middleware
    async def setup_boundary(request: web.Request, handler):
        methods = _SETUP_PATHS.get(request.path)
        if methods is None:
            return await handler(request)
        preflight = request.method == "OPTIONS"
        headers = _cors_headers(request, server, preflight=preflight)
        if headers is None:
            return web.json_response(
                {"error": "PERSONAL_CHAT_ORIGIN_FORBIDDEN"},
                status=403,
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        if preflight:
            requested = request.headers.get("Access-Control-Request-Method", "").upper()
            if requested and requested not in methods:
                return web.Response(status=405, headers=headers)
            return web.Response(status=204, headers=headers)
        if request.method not in methods:
            return web.json_response({"error": "METHOD_NOT_ALLOWED"}, status=405, headers=headers)
        if request.headers.get(CONFIRM_HEADER) != CONFIRM_VALUE:
            return web.json_response(
                {"error": "PERSONAL_CHAT_CONFIRM_REQUIRED"},
                status=403,
                headers=headers,
            )
        try:
            response = await endpoints[request.path](request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = web.json_response({"error": _failure_code(exc)}, status=503)
        response.headers.update(headers)
        return response

    async def cleanup(application: web.Application) -> None:
        for name in ("wechat_task", "napcat_task", "napcat_login_task", "napcat_watchdog_task"):
            task = runtime.get(name)
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app.middlewares.append(setup_boundary)
    app.on_startup.append(start_managed_napcat)
    app.on_cleanup.append(cleanup)


__all__ = [
    "CHANNEL_CHOICE_PATH",
    "CONFIRM_HEADER",
    "CONFIRM_VALUE",
    "NAPCAT_INSTALL_PATH",
    "NAPCAT_START_PATH",
    "QQ_CONFIGURE_PATH",
    "STATUS_PATH",
    "WECHAT_START_PATH",
    "WECHAT_VERIFY_PATH",
    "install_setup_routes",
]
