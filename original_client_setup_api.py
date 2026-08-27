"""Credential-safe first-run LLM setup for the original Olivia client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web


SETUP_STATUS_PATH = "/toy/setup/status"
LLM_TEST_PATH = "/toy/setup/llm/test"
LLM_SAVE_PATH = "/toy/setup/llm/save"
LLM_DELETE_PATH = "/toy/setup/llm/delete"
SETUP_COMPLETE_PATH = "/toy/setup/complete"
CONFIRM_HEADER = "X-Olivia-Setup-Action"
CONFIRM_VALUE = "confirmed"
_MAX_BODY_BYTES = 4_096
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_LOOPBACK_ORIGIN_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$")
_SERVICE_KEY = web.AppKey("original_client_setup_service", object)
_ORIGINS_KEY = web.AppKey("original_client_setup_origins", frozenset)
_MOUNTED_KEY = web.AppKey("original_client_setup_mounted", bool)
Probe = Callable[[str, str, str], Awaitable[None]]
Protector = Callable[[str], str]


class LLMSetupError(RuntimeError):
    """Stable setup failure that never contains provider or credential data."""

    def __init__(self, code: str, *, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _dpapi_protect(value: str) -> str:
    if os.name != "nt":
        raise LLMSetupError("LLM_SETUP_DPAPI_UNAVAILABLE", status=503)
    script = (
        "Add-Type -AssemblyName System.Security; "
        "$b=[Text.Encoding]::UTF8.GetBytes([Console]::In.ReadToEnd()); "
        "$p=[Security.Cryptography.ProtectedData]::Protect($b,$null,"
        "[Security.Cryptography.DataProtectionScope]::CurrentUser); "
        "[Convert]::ToBase64String($p)"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        input=value,
        text=True,
        capture_output=True,
        check=False,
    )
    protected = result.stdout.strip()
    if result.returncode or not protected:
        raise LLMSetupError("LLM_SETUP_DPAPI_FAILED", status=503)
    return f"dpapi-v1:{protected}"


def _dpapi_unprotect(value: str) -> str:
    if os.name != "nt":
        raise LLMSetupError("LLM_SETUP_DPAPI_UNAVAILABLE", status=503)
    modern = value.startswith("dpapi-v1:")
    script = (
        "Add-Type -AssemblyName System.Security; "
        "$p=[Convert]::FromBase64String([Console]::In.ReadToEnd()); "
        "$b=[Security.Cryptography.ProtectedData]::Unprotect($p,$null,"
        "[Security.Cryptography.DataProtectionScope]::CurrentUser); "
        "[Text.Encoding]::UTF8.GetString($b)"
        if modern
        else (
            "$s=ConvertTo-SecureString ([Console]::In.ReadToEnd()); "
            "$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); "
            "try {[Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)} "
            "finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}"
        )
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        input=value.removeprefix("dpapi-v1:"),
        text=True,
        capture_output=True,
        check=False,
    )
    secret = result.stdout.strip()
    if result.returncode or not secret:
        raise LLMSetupError("LLM_SETUP_KEY_UNAVAILABLE", status=503)
    return secret


def _base_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400) from exc
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        not parsed.hostname
        or parsed.scheme not in ({"http", "https"} if loopback else {"https"})
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
    return normalized


def _model(value: object) -> str:
    if not isinstance(value, str) or not _MODEL_RE.fullmatch(value.strip()):
        raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
    return value.strip()


def _api_key(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
    normalized = value.strip()
    if not normalized and allow_empty:
        return ""
    if (
        not 8 <= len(normalized) <= 512
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
    return normalized


async def _probe_openai_compatible(base_url: str, model: str, api_key: str) -> None:
    try:
        async with ClientSession(timeout=ClientTimeout(total=20)) as session:
            async with session.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 2,
                    "stream": False,
                },
            ) as response:
                if not 200 <= response.status < 300:
                    raise LLMSetupError("LLM_SETUP_CONNECTION_FAILED", status=503)
                payload = await response.json(content_type=None)
    except LLMSetupError:
        raise
    except (ClientError, TimeoutError, ValueError) as exc:
        raise LLMSetupError("LLM_SETUP_CONNECTION_FAILED", status=503) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
        raise LLMSetupError("LLM_SETUP_CONNECTION_FAILED", status=503)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".staging")
    staging.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    staging.replace(path)


class LLMSetupService:
    """Own first-run state and persist only DPAPI ciphertext plus public config."""

    def __init__(
        self,
        data_root: Path,
        *,
        protect: Protector = _dpapi_protect,
        unprotect: Protector = _dpapi_unprotect,
        probe: Probe = _probe_openai_compatible,
    ) -> None:
        self._config_root = Path(data_root) / "config"
        self._key_path = self._config_root / "deepseek_api_key.dpapi"
        self._config_path = self._config_root / "llm.json"
        self._complete_path = self._config_root / "initial_setup.json"
        self._protect = protect
        self._unprotect = unprotect
        self._probe = probe
        self._login_observed = False
        self._tested_digest: str | None = None

    def observe_login(self, *, success: bool) -> None:
        if success:
            self._login_observed = True

    def _config(self) -> dict[str, object]:
        fallback: dict[str, object] = {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
        }
        try:
            payload = json.loads(self._config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                return fallback
            return {
                "base_url": _base_url(payload.get("base_url")),
                "model": _model(payload.get("model")),
            }
        except (OSError, UnicodeError, json.JSONDecodeError, LLMSetupError):
            return fallback

    def _completion(self) -> tuple[bool, bool]:
        try:
            payload = json.loads(self._complete_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, False
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return False, False
        return payload.get("completed") is True, payload.get("skipped") is True

    def status(self) -> dict[str, object]:
        completed, skipped = self._completion()
        config = self._config()
        return {
            "schema_version": "olivia.initial-setup.v1",
            "status": "READY",
            "login_observed": self._login_observed,
            "setup_completed": completed,
            "show_initial_setup": self._login_observed and not completed,
            "skipped": skipped,
            "llm": {
                "base_url": config["base_url"],
                "model": config["model"],
                "key_configured": self._key_path.is_file(),
            },
        }

    @staticmethod
    def _digest(base_url: str, model: str, api_key: str) -> str:
        return hashlib.sha256(
            "\0".join((base_url, model, api_key)).encode("utf-8")
        ).hexdigest()

    def _secret(self, supplied: object) -> str:
        candidate = _api_key(supplied, allow_empty=True)
        if candidate:
            return candidate
        try:
            protected = self._key_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LLMSetupError("LLM_SETUP_KEY_REQUIRED", status=400) from exc
        if not protected:
            raise LLMSetupError("LLM_SETUP_KEY_REQUIRED", status=400)
        return _api_key(self._unprotect(protected))

    async def test(self, payload: dict[str, object]) -> None:
        if set(payload) != {"base_url", "model", "api_key"}:
            raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
        base_url = _base_url(payload.get("base_url"))
        model = _model(payload.get("model"))
        api_key = self._secret(payload.get("api_key", ""))
        await self._probe(base_url, model, api_key)
        self._tested_digest = self._digest(base_url, model, api_key)

    def save(self, payload: dict[str, object]) -> None:
        if set(payload) != {"base_url", "model", "api_key"}:
            raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
        base_url = _base_url(payload.get("base_url"))
        model = _model(payload.get("model"))
        api_key = self._secret(payload.get("api_key", ""))
        if self._tested_digest != self._digest(base_url, model, api_key):
            raise LLMSetupError("LLM_SETUP_TEST_REQUIRED", status=409)
        protected = self._protect(api_key)
        self._config_root.mkdir(parents=True, exist_ok=True)
        key_staging = self._key_path.with_suffix(self._key_path.suffix + ".staging")
        key_staging.write_text(protected + "\n", encoding="utf-8")
        _atomic_json(
            self._config_path,
            {"schema_version": 1, "base_url": base_url, "model": model},
        )
        key_staging.replace(self._key_path)
        self._tested_digest = None

    def delete(self) -> None:
        try:
            self._key_path.unlink()
        except FileNotFoundError:
            pass
        self._tested_digest = None

    def complete(self, *, skipped: object) -> bool:
        if type(skipped) is not bool:
            raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
        _atomic_json(
            self._complete_path,
            {"schema_version": 1, "completed": True, "skipped": skipped},
        )
        return skipped


def _normalize_origins(values: Sequence[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for value in values:
        candidate = value.rstrip("/") if isinstance(value, str) else ""
        try:
            parsed = urlsplit(candidate)
        except ValueError as exc:
            raise ValueError("trusted origins are invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or candidate in normalized
        ):
            raise ValueError("trusted origins are invalid")
        normalized.add(candidate)
    return frozenset(normalized)


def _authorize(request: web.Request, *, confirm: bool) -> str:
    try:
        hostname = urlsplit(f"//{request.host}").hostname
    except ValueError as exc:
        raise LLMSetupError("LLM_SETUP_HOST_FORBIDDEN", status=403) from exc
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise LLMSetupError("LLM_SETUP_HOST_FORBIDDEN", status=403)
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin not in request.app[_ORIGINS_KEY] and not _LOOPBACK_ORIGIN_RE.fullmatch(origin):
        raise LLMSetupError("LLM_SETUP_ORIGIN_FORBIDDEN", status=403)
    if confirm and request.headers.get(CONFIRM_HEADER) != CONFIRM_VALUE:
        raise LLMSetupError("LLM_SETUP_CONFIRMATION_REQUIRED", status=403)
    return origin


def _headers(origin: str | None = None, *, preflight: bool = False) -> dict[str, str]:
    values = {"Cache-Control": "no-store"}
    if origin:
        values["Access-Control-Allow-Origin"] = origin
        values["Vary"] = "Origin"
    if preflight:
        values.update(
            {
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": f"Content-Type, {CONFIRM_HEADER}",
                "Access-Control-Max-Age": "600",
            }
        )
    return values


async def _body(request: web.Request) -> dict[str, object]:
    if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:
        raise LLMSetupError("LLM_SETUP_REQUEST_TOO_LARGE", status=413)
    if request.content_type != "application/json":
        raise LLMSetupError("LLM_SETUP_CONTENT_TYPE_INVALID", status=415)
    raw = await request.content.read(_MAX_BODY_BYTES + 1)
    if len(raw) > _MAX_BODY_BYTES:
        raise LLMSetupError("LLM_SETUP_REQUEST_TOO_LARGE", status=413)
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LLMSetupError("LLM_SETUP_JSON_INVALID", status=400) from exc
    if not isinstance(payload, dict):
        raise LLMSetupError("LLM_SETUP_JSON_INVALID", status=400)
    return payload


def mount_original_client_setup_api(
    app: web.Application,
    service: LLMSetupService,
    *,
    trusted_origins: Sequence[str] = (),
) -> None:
    if app.get(_MOUNTED_KEY, False):
        raise RuntimeError("LLM_SETUP_ALREADY_MOUNTED")
    app[_SERVICE_KEY] = service
    app[_ORIGINS_KEY] = _normalize_origins(trusted_origins)
    app[_MOUNTED_KEY] = True

    async def options(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=False)
        return web.Response(status=204, headers=_headers(origin, preflight=True))

    async def status(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=False)
        return web.json_response(service.status(), headers=_headers(origin))

    async def test(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=True)
        await service.test(await _body(request))
        return web.json_response({"status": "AVAILABLE"}, headers=_headers(origin))

    async def save(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=True)
        service.save(await _body(request))
        return web.json_response(
            {"status": "SAVED", "restart_required": True}, headers=_headers(origin)
        )

    async def delete(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=True)
        if await _body(request):
            raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
        service.delete()
        return web.json_response(
            {"status": "DELETED", "restart_required": True}, headers=_headers(origin)
        )

    async def complete(request: web.Request) -> web.Response:
        origin = _authorize(request, confirm=True)
        payload = await _body(request)
        if set(payload) != {"skipped"}:
            raise LLMSetupError("LLM_SETUP_FIELDS_INVALID", status=400)
        skipped = service.complete(skipped=payload["skipped"])
        return web.json_response(
            {"status": "COMPLETED", "skipped": skipped}, headers=_headers(origin)
        )

    @web.middleware
    async def errors(request: web.Request, handler: Callable[..., Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        try:
            return await handler(request)
        except LLMSetupError as exc:
            return web.json_response(
                {"status": "FAILED", "error_code": exc.code},
                status=exc.status,
                headers=_headers(),
            )

    app.middlewares.append(errors)
    app.router.add_get(SETUP_STATUS_PATH, status)
    for path, handler in (
        (LLM_TEST_PATH, test),
        (LLM_SAVE_PATH, save),
        (LLM_DELETE_PATH, delete),
        (SETUP_COMPLETE_PATH, complete),
    ):
        app.router.add_post(path, handler)
    for path in (
        SETUP_STATUS_PATH,
        LLM_TEST_PATH,
        LLM_SAVE_PATH,
        LLM_DELETE_PATH,
        SETUP_COMPLETE_PATH,
    ):
        app.router.add_options(path, options)


__all__ = ["LLMSetupError", "LLMSetupService", "mount_original_client_setup_api"]
