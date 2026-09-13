"""Loopback-only actions for user-downloaded local Olivia patches."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
import json
import hashlib
import os
import re
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from aiohttp import web

from installer.component_update import (
    ComponentUpdateError,
    apply_component_update,
    rollback_component_update,
)


ACTION_PATH = "/toy/updates/local/action"
STATUS_PATH = "/toy/updates/local/status"
CONFIRM_HEADER = "X-Olivia-Update-Action"
SESSION_HEADER = "X-Olivia-Setup-Session"
_MAX_JSON_BYTES = 8_192
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
_LOOPBACK_ORIGIN_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$")
_ORIGINS_KEY = web.AppKey("original_client_update_origins", frozenset)
_MOUNTED_KEY = web.AppKey("original_client_update_mounted", bool)
_REPARSE_POINT = 0x0400


class UpdateAPIError(RuntimeError):
    def __init__(self, code: str, *, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class ComponentUpdater(Protocol):
    def apply(self, package: Path, manifest_sha256: str) -> Mapping[str, object]: ...

    def rollback(self) -> Mapping[str, object]: ...


SessionAuthorizer = Callable[[str], None]
PatchPicker = Callable[[], Path | None]


class _ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if (target.scheme != "https" or target.hostname not in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
        } or target.username or target.password or target.port not in {None, 443}):
            raise UpdateAPIError("UPDATE_CHECKSUM_UNAVAILABLE", status=503)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _official_manifest_digest(package: Path) -> str:
    # The untrusted version only selects a fixed project release. The installer
    # still checks the manifest against this external digest before staging files.
    try:
        with zipfile.ZipFile(package) as archive:
            entries = [entry for entry in archive.infolist() if entry.filename == "manifest.json"]
            if len(entries) != 1 or entries[0].file_size > 1_048_576:
                raise ValueError("invalid manifest")
            with archive.open(entries[0]) as source:
                raw = source.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("oversized manifest")
            manifest = json.loads(raw)
            version = manifest.get("version") if isinstance(manifest, dict) else None
            if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
                raise ValueError("invalid version")
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise UpdateAPIError("UPDATE_MANIFEST_INVALID", status=409) from exc
    url = (
        "https://github.com/Ornn8/bside-olivia-community/releases/download/"
        f"v{version}/Olivia-{version}.oliviapatch.manifest.sha256"
    )
    try:
        request = Request(url, headers={"User-Agent": "Olivia-Updater", "Accept": "text/plain"})
        with build_opener(_ReleaseRedirects()).open(request, timeout=20) as response:
            raw_digest = response.read(257)
        digest = raw_digest.decode("ascii").strip()
        if len(raw_digest) > 256 or not _SHA256_RE.fullmatch(digest):
            raise ValueError("invalid checksum")
        return digest
    except Exception as exc:
        raise UpdateAPIError("UPDATE_CHECKSUM_UNAVAILABLE", status=503) from exc


def running_component_version(backend_root: Path | None = None) -> dict[str, str | None]:
    # The selected update can differ until restart. Report this loaded module.
    root = backend_root if backend_root is not None else Path(__file__).resolve().parent
    match = re.fullmatch(r"([0-9A-Za-z][0-9A-Za-z.+-]{0,63})-([0-9a-f]{64})", root.name)
    if match and root.parent.name == "local_backend" and root.parent.parent.name == "versions":
        return {"version": match[1], "manifest_sha256": match[2]}
    try:
        release = json.loads((root / "installer" / "release-version.json").read_text(encoding="utf-8"))
        version = release.get("version") if isinstance(release, dict) else None
        if isinstance(version, str) and _VERSION_RE.fullmatch(version):
            return {"version": version, "manifest_sha256": None}
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return {"version": None, "manifest_sha256": None}


class LocalComponentUpdater:
    def __init__(self, installation: Path) -> None:
        if not installation.is_absolute():
            raise ValueError("an absolute installation path is required")
        self._installation = installation.resolve()

    def apply(self, package: Path, manifest_sha256: str) -> Mapping[str, object]:
        return apply_component_update(
            self._installation,
            package,
            expected_manifest_sha256=manifest_sha256,
        )

    def rollback(self) -> Mapping[str, object]:
        return rollback_component_update(self._installation)


def _normalize_origins(values: Sequence[str]) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        candidate = value.rstrip("/") if isinstance(value, str) else ""
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("trusted origins are invalid")
        result.add(candidate)
    return frozenset(result)


def _authorize(request: web.Request, *, confirmation: bool) -> str:
    try:
        hostname = urlsplit(f"//{request.host}").hostname
    except ValueError as exc:
        raise UpdateAPIError("UPDATE_HOST_FORBIDDEN", status=403) from exc
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise UpdateAPIError("UPDATE_HOST_FORBIDDEN", status=403)
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin not in request.app[_ORIGINS_KEY] and not _LOOPBACK_ORIGIN_RE.fullmatch(origin):
        raise UpdateAPIError("UPDATE_ORIGIN_FORBIDDEN", status=403)
    if confirmation and request.headers.get(CONFIRM_HEADER) != "confirmed":
        raise UpdateAPIError("UPDATE_CONFIRMATION_REQUIRED", status=403)
    return origin


def _headers(origin: str | None = None, *, preflight: bool = False) -> dict[str, str]:
    headers = {"Cache-Control": "no-store"}
    if origin:
        headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
    if preflight:
        headers.update(
            {
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": (
                    f"Content-Type, {CONFIRM_HEADER}, {SESSION_HEADER}"
                ),
                "Access-Control-Max-Age": "600",
            }
        )
    return headers


async def _json_body(request: web.Request) -> dict[str, object]:
    if request.content_length is not None and request.content_length > _MAX_JSON_BYTES:
        raise UpdateAPIError("UPDATE_REQUEST_TOO_LARGE", status=413)
    if request.content_type != "application/json":
        raise UpdateAPIError("UPDATE_CONTENT_TYPE_INVALID", status=415)
    raw = await request.content.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise UpdateAPIError("UPDATE_REQUEST_TOO_LARGE", status=413)
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateAPIError("UPDATE_JSON_INVALID", status=400) from exc
    if not isinstance(payload, dict):
        raise UpdateAPIError("UPDATE_JSON_INVALID", status=400)
    return payload


def _package_path(value: object, *, allow_bundle: bool = False) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
    path = Path(value).expanduser()
    if not path.is_absolute() or path.suffix.casefold() not in ({".oliviapatch", ".zip"} if allow_bundle else {".oliviapatch"}):
        raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
    try:
        metadata = path.lstat()
        if path.is_symlink() or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ) or not path.is_file():
            raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
        return path.resolve(strict=True)
    except OSError as exc:
        raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400) from exc


def _apply_update_bundle(updater: ComponentUpdater, bundle: Path) -> Mapping[str, object]:
    # Bundled hashes check transfer integrity; they are not a publisher signature.
    with tempfile.TemporaryDirectory(prefix="olivia-update-", ignore_cleanup_errors=True) as directory:
        package = Path(directory) / "update.oliviapatch"
        try:
            with zipfile.ZipFile(bundle) as archive:
                entries = archive.infolist()
                patches = [entry for entry in entries if entry.filename.casefold().endswith('.oliviapatch')]
                if len(entries) != 3 or len(patches) != 1:
                    raise ValueError()
                patch = patches[0]
                name = patch.filename
                if (not 0 < patch.file_size <= 512 * 1024 * 1024 or len(name) > 256
                    or '/' in name or '\\' in name
                    or {entry.filename for entry in entries} != {name, name + '.sha256', name + '.manifest.sha256'}
                    or any(entry.is_dir() or entry.flag_bits & 1
                           or (entry.external_attr >> 16) & 0o170000 not in {0, 0o100000} for entry in entries)):
                    raise ValueError()
                digests = []
                for suffix in ('.sha256', '.manifest.sha256'):
                    entry = archive.getinfo(name + suffix)
                    if entry.file_size > 256:
                        raise ValueError()
                    value = archive.read(entry).decode('ascii').strip()
                    if not _SHA256_RE.fullmatch(value):
                        raise ValueError()
                    digests.append(value)
                digest = hashlib.sha256()
                count = 0
                with archive.open(patch) as source, package.open('xb') as output:
                    while chunk := source.read(1024 * 1024):
                        count += len(chunk)
                        if count > patch.file_size:
                            raise ValueError()
                        digest.update(chunk)
                        output.write(chunk)
                if count != patch.file_size or digest.hexdigest() != digests[0]:
                    raise UpdateAPIError('UPDATE_BUNDLE_CHECKSUM_MISMATCH', status=409)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
            if isinstance(exc, UpdateAPIError):
                raise
            raise UpdateAPIError('UPDATE_BUNDLE_INVALID', status=409) from exc
        return updater.apply(package, digests[1])


def _select_windows_patch() -> Path | None:
    if os.name != "nt":
        raise UpdateAPIError("UPDATE_PICKER_UNAVAILABLE", status=503)
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$owner = New-Object System.Windows.Forms.Form;"
        "$dialog = New-Object System.Windows.Forms.OpenFileDialog;"
        "try {"
        "$owner.TopMost = $true;"
        "$owner.ShowInTaskbar = $false;"
        "$owner.Opacity = 0;"
        "$owner.Width = 1; $owner.Height = 1;"
        "$owner.StartPosition = 'CenterScreen';"
        "$owner.Show();"
        "$owner.Activate();"
        "$dialog.Filter = 'Olivia update (*.zip;*.oliviapatch)|*.zip;*.oliviapatch';"
        "$dialog.CheckFileExists = $true;"
        "$dialog.Multiselect = $false;"
        "if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($dialog.FileName) }"
        "} finally { $dialog.Dispose(); $owner.Dispose(); }"
    )
    try:
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateAPIError("UPDATE_PICKER_UNAVAILABLE", status=503) from exc
    if completed.returncode != 0:
        raise UpdateAPIError("UPDATE_PICKER_UNAVAILABLE", status=503)
    selected = completed.stdout.strip()
    return None if not selected else _package_path(selected, allow_bundle=True)


def _public_result(value: Mapping[str, object]) -> dict[str, object]:
    status = value.get("status")
    component = value.get("component")
    version = value.get("version")
    if (
        status not in {"APPLIED", "ROLLED_BACK"}
        or component != "local_backend"
        or not isinstance(version, str)
        or not _VERSION_RE.fullmatch(version)
    ):
        raise UpdateAPIError("UPDATE_RESULT_INVALID", status=503)
    return {
        "status": status,
        "component": component,
        "version": version,
        "restart_required": True,
    }


def mount_original_client_update_api(
    app: web.Application,
    updater: ComponentUpdater,
    *,
    trusted_origins: Sequence[str],
    authorize_session: SessionAuthorizer,
    select_patch: PatchPicker | None = None,
) -> None:
    if app.get(_MOUNTED_KEY, False):
        raise RuntimeError("UPDATE_API_ALREADY_MOUNTED")
    if not callable(authorize_session):
        raise TypeError("a setup session authorizer is required")
    app[_ORIGINS_KEY] = _normalize_origins(trusted_origins)
    app[_MOUNTED_KEY] = True
    control_lock = asyncio.Lock()
    picker = select_patch or _select_windows_patch

    def apply_verified(package: Path) -> Mapping[str, object]:
        if package.suffix.casefold() == '.zip':
            return _apply_update_bundle(updater, package)
        return updater.apply(package, _official_manifest_digest(package))

    @web.middleware
    async def errors(request: web.Request, handler):
        try:
            return await handler(request)
        except UpdateAPIError as exc:
            try:
                origin = _authorize(request, confirmation=False)
            except UpdateAPIError:
                origin = None
            return web.json_response(
                {"status": "FAILED", "error_code": exc.code},
                status=exc.status,
                headers=_headers(origin),
            )

    app.middlewares.append(errors)

    async def options(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=False)
        return web.Response(status=204, headers=_headers(origin, preflight=True))

    async def action(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=True)
        try:
            authorize_session(request.headers.get(SESSION_HEADER, ""))
        except Exception as exc:
            raise UpdateAPIError("UPDATE_LOGIN_REQUIRED", status=403) from exc
        payload = await _json_body(request)
        if payload == {"action": "select"}:
            try:
                selected = await asyncio.to_thread(picker)
                result = (
                    {"status": "CANCELLED", "restart_required": False}
                    if selected is None
                    else {
                        "status": "SELECTED",
                        "package_path": str(_package_path(str(selected), allow_bundle=True)),
                        "restart_required": False,
                    }
                )
            except UpdateAPIError:
                raise
            except Exception as exc:
                raise UpdateAPIError("UPDATE_PICKER_UNAVAILABLE", status=503) from exc
            return web.json_response(result, headers=_headers(origin))
        if payload.get("action") == "apply" and set(payload) == {
            "action", "package_path", "manifest_sha256"
        }:
            digest = payload.get("manifest_sha256")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
            call = updater.apply
            args = (_package_path(payload.get("package_path")), digest)
        elif payload.get("action") == "apply_verified" and set(payload) == {"action", "package_path"}:
            call = apply_verified
            args = (_package_path(payload.get("package_path"), allow_bundle=True),)
        elif payload == {"action": "rollback"}:
            call = updater.rollback
            args = ()
        else:
            raise UpdateAPIError("UPDATE_FIELDS_INVALID", status=400)
        try:
            async with control_lock:
                result = await asyncio.to_thread(call, *args)
        except UpdateAPIError:
            raise
        except ComponentUpdateError as exc:
            code = str(exc)
            if not re.fullmatch(r"UPDATE_[A-Z0-9_]{3,90}", code):
                code = "UPDATE_ACTION_UNAVAILABLE"
            raise UpdateAPIError(code, status=409) from exc
        except Exception as exc:
            raise UpdateAPIError("UPDATE_ACTION_UNAVAILABLE", status=503) from exc
        return web.json_response(_public_result(result), headers=_headers(origin))

    async def status(request: web.Request) -> web.Response:
        origin = _authorize(request, confirmation=False)
        return web.json_response({"status": "READY", **running_component_version()}, headers=_headers(origin))

    app.router.add_get(STATUS_PATH, status)
    app.router.add_options(STATUS_PATH, options)
    app.router.add_post(ACTION_PATH, action)
    app.router.add_options(ACTION_PATH, options)


__all__ = [
    "ACTION_PATH",
    "CONFIRM_HEADER",
    "LocalComponentUpdater",
    "PatchPicker",
    "SESSION_HEADER",
    "UpdateAPIError",
    "mount_original_client_update_api",
]
