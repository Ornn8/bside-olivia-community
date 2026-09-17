"""Local, user-confirmed QQ/Weixin binding for personal chat.

The setup surface deliberately keeps credentials on this Windows installation.
It only exposes content-free status plus a short-lived locally rendered Weixin QR.
"""
from __future__ import annotations

import base64
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from .probe import checked_url, wechat_request
from .qr_svg import QRPayloadTooLong, svg_bytes


STATUS_PATH = "/toy/personal-chat/setup/status"
WECHAT_START_PATH = "/toy/personal-chat/setup/wechat/start"
WECHAT_VERIFY_PATH = "/toy/personal-chat/setup/wechat/verify"
QQ_CONFIGURE_PATH = "/toy/personal-chat/setup/qq/configure"
CONFIRM_HEADER = "X-Olivia-Companion-Action"
CONFIRM_VALUE = "confirmed"
_SETUP = web.AppKey("personal_chat_setup", dict)
_QQ_ID = re.compile(r"^[1-9][0-9]{4,19}$")
_VERIFY_CODE = re.compile(r"^[0-9]{4,8}$")


def _failure_code(exc: BaseException) -> str:
    code = str(exc)
    if re.fullmatch(r"(?:PERSONAL_CHAT|WECHAT|QQ)_[A-Z0-9_]{1,80}", code):
        return code
    return "PERSONAL_CHAT_SETUP_UNAVAILABLE"


def _confirmed(request: web.Request) -> None:
    if request.headers.get(CONFIRM_HEADER) != CONFIRM_VALUE:
        raise web.HTTPForbidden(
            text=json.dumps({"error": "PERSONAL_CHAT_CONFIRM_REQUIRED"}),
            content_type="application/json",
        )


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
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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
    _atomic_text(_config_path(server), json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _selected_channels(server) -> set[str]:
    # Import lazily so backend can install these routes without a module cycle.
    from .backend import selected_channels

    return selected_channels(server)


def _contact_state(server) -> str:
    from .contact_invitation import status

    port = getattr(server, "private_world_port", None)
    snapshot = port.snapshot() if port is not None else None
    return str(status(server.store.letters, snapshot).get("state", "locked"))


def _qr_content(value: object) -> str:
    """Validate official QR content before encoding it locally.

    Tencent currently returns a Weixin HTTPS target, but the protocol permits
    display content rather than an image URL. Nothing here is fetched by the UI.
    """
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
    from . import backend

    active = request.app.get(backend._RUNTIME)
    listener = dict(active.get("status", {})) if isinstance(active, dict) else {}
    runtime = request.app[_SETUP]
    wechat = dict(runtime.get("wechat", {"state": "IDLE"}))
    # Never return login identifiers, tokens, config paths, or the QR polling key.
    wechat.pop("qrcode", None)
    wechat.pop("verify_code", None)
    qq = dict(runtime.get("qq", {"state": "IDLE"}))
    return {
        "contact_state": _contact_state(server),
        "selected_channels": sorted(selected),
        "configured": configured,
        "listeners": {name: listener.get(name, "CONFIGURED_RESTART" if configured[name] else "SETUP_REQUIRED")
                      for name in selected},
        "wechat": wechat,
        "qq": qq,
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
                login = await wechat_request(session, base, "/ilink/bot/get_qrcode_status", params=params)
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
                    _atomic_text(secret, _dpapi_protect(json.dumps(credentials, ensure_ascii=False)))
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
                    base = checked_url(host if str(host).startswith("https://") else "https://" + str(host))
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


async def _qq_probe(url: str, token: str, account: str) -> None:
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
                if raw.get("status") != "ok" or raw.get("retcode") != 0 or not isinstance(raw.get("data"), dict):
                    raise RuntimeError("QQ_LOGIN_UNAVAILABLE")
                if str(raw["data"].get("user_id")) != account:
                    raise RuntimeError("QQ_ACCOUNT_MISMATCH")
                return
    raise RuntimeError("QQ_LOGIN_UNAVAILABLE")


def install_setup_routes(app: web.Application, server) -> None:
    if _SETUP in app:
        return
    runtime: dict[str, object] = {
        "wechat": {"state": "IDLE"},
        "qq": {"state": "IDLE"},
        "wechat_task": None,
    }
    app[_SETUP] = runtime

    async def status(request: web.Request) -> web.Response:
        _confirmed(request)
        try:
            return web.json_response(_public_status(request, server))
        except Exception as exc:
            return web.json_response({"error": _failure_code(exc)}, status=503)

    async def wechat_start(request: web.Request) -> web.Response:
        _confirmed(request)
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
        _confirmed(request)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "WECHAT_VERIFY_CODE_INVALID"}, status=400)
        code = body.get("code") if isinstance(body, dict) else None
        state = runtime.get("wechat")
        if not isinstance(state, dict) or state.get("state") != "VERIFY_REQUIRED" or not isinstance(code, str) or not _VERIFY_CODE.fullmatch(code):
            return web.json_response({"error": "WECHAT_VERIFY_CODE_INVALID"}, status=400)
        state["verify_code"] = code
        state["state"] = "SCANNED"
        return web.json_response({"status": "VERIFYING"})

    async def qq_configure(request: web.Request) -> web.Response:
        _confirmed(request)
        if "qq" not in _selected_channels(server):
            return web.json_response({"error": "PERSONAL_CHAT_CONTACT_NOT_ACCEPTED"}, status=409)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "QQ_SETUP_INVALID"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "QQ_SETUP_INVALID"}, status=400)
        account = str(body.get("account", "")).strip()
        owner = str(body.get("owner", "")).strip()
        token = str(body.get("token", ""))
        url = str(body.get("url", "ws://127.0.0.1:3001")).strip()
        try:
            parsed = urlsplit(checked_url(url, local=True))
            if parsed.scheme != "ws" or not _QQ_ID.fullmatch(account) or not _QQ_ID.fullmatch(owner):
                raise RuntimeError("QQ_SETUP_INVALID")
            if account == owner:
                raise RuntimeError("QQ_BOT_AND_OWNER_MUST_DIFFER")
            if not 16 <= len(token) <= 512:
                raise RuntimeError("QQ_TOKEN_INVALID")
            runtime["qq"] = {"state": "TESTING"}
            await _qq_probe(url, token, account)
            from original_client_setup_api import _dpapi_protect

            secret = _root(server) / "personal-chat" / "qq.dpapi"
            _atomic_text(secret, _dpapi_protect(json.dumps({"token": token}, ensure_ascii=False)))
            config = _read_config(server)
            config["qq"] = {
                "url": url,
                "account": account,
                "owner": owner,
                "credentials_file": str(secret),
            }
            _write_config(server, config)
            runtime["qq"] = {"state": "READY_RESTART"}
            return web.json_response({"status": "READY_RESTART"})
        except Exception as exc:
            code = _failure_code(exc)
            runtime["qq"] = {"state": "FAILED", "error": code}
            return web.json_response({"error": code}, status=400 if code.startswith("QQ_") else 503)

    async def cleanup(application: web.Application) -> None:
        task = runtime.get("wechat_task")
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app.router.add_get(STATUS_PATH, status)
    app.router.add_post(WECHAT_START_PATH, wechat_start)
    app.router.add_post(WECHAT_VERIFY_PATH, wechat_verify)
    app.router.add_post(QQ_CONFIGURE_PATH, qq_configure)
    app.on_cleanup.append(cleanup)


__all__ = [
    "CONFIRM_HEADER",
    "CONFIRM_VALUE",
    "QQ_CONFIGURE_PATH",
    "STATUS_PATH",
    "WECHAT_START_PATH",
    "WECHAT_VERIFY_PATH",
    "install_setup_routes",
]
