"""Verified local bootstrap for the optional NapCat QQ transport."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import urllib.request
from urllib.parse import quote, urlsplit
import webbrowser
import zipfile


NAPCAT_VERSION = "v4.18.28"
NAPCAT_ASSET = "NapCat.Shell.Windows.OneKey.zip"
NAPCAT_URL = (
    "https://github.com/NapNeko/NapCatQQ/releases/download/"
    f"{NAPCAT_VERSION}/{NAPCAT_ASSET}"
)
NAPCAT_SHA256 = "fa365537039e9ec29730166f3f624eb147074be18be64d1981a03f35ecb2a2af"
NAPCAT_SOURCE = "NapNeko/NapCatQQ"
NAPCAT_LICENSE = "Limited Redistribution License for NapCat"
NAPCAT_WS_URL = "ws://127.0.0.1:3001"
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 32 * 1024 * 1024
_MAX_MEMBERS = 96
_ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
})


class NapCatSetupError(RuntimeError):
    pass


def _root(data_root: Path) -> Path:
    root = Path(data_root) / "personal-chat" / "napcat"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _allowed_download_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (
            host in _ALLOWED_DOWNLOAD_HOSTS
            or host.endswith(".githubusercontent.com")
        )
    )


def _download_archive(path: Path) -> None:
    if not _allowed_download_url(NAPCAT_URL):
        raise NapCatSetupError("NAPCAT_SOURCE_INVALID")
    request = urllib.request.Request(
        NAPCAT_URL,
        headers={"User-Agent": "Olivia-local-NapCat-bootstrap/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            final_url = response.geturl()
            if not _allowed_download_url(final_url):
                raise NapCatSetupError("NAPCAT_SOURCE_INVALID")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > _MAX_ARCHIVE_BYTES:
                raise NapCatSetupError("NAPCAT_ARCHIVE_TOO_LARGE")
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".napcat-", suffix=".zip", dir=path.parent)
            temporary = Path(name)
            total = 0
            digest = hashlib.sha256()
            try:
                with os.fdopen(fd, "wb") as stream:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _MAX_ARCHIVE_BYTES:
                            raise NapCatSetupError("NAPCAT_ARCHIVE_TOO_LARGE")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if digest.hexdigest().lower() != NAPCAT_SHA256:
                    raise NapCatSetupError("NAPCAT_ARCHIVE_HASH_MISMATCH")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    except NapCatSetupError:
        raise
    except Exception as exc:
        raise NapCatSetupError("NAPCAT_DOWNLOAD_FAILED") from exc


def _safe_extract(archive_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if not infos or len(infos) > _MAX_MEMBERS:
                raise NapCatSetupError("NAPCAT_ARCHIVE_INVALID")
            if sum(max(0, info.file_size) for info in infos) > _MAX_EXTRACTED_BYTES:
                raise NapCatSetupError("NAPCAT_ARCHIVE_TOO_LARGE")
            root = destination.resolve()
            staging = destination.with_name(destination.name + ".staging")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            try:
                stage_root = staging.resolve()
                for info in infos:
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise NapCatSetupError("NAPCAT_ARCHIVE_INVALID")
                    name = info.filename.replace("\\", "/")
                    if name.startswith("/") or ":" in name.split("/", 1)[0]:
                        raise NapCatSetupError("NAPCAT_ARCHIVE_INVALID")
                    target = (staging / name).resolve()
                    if target != stage_root and stage_root not in target.parents:
                        raise NapCatSetupError("NAPCAT_ARCHIVE_INVALID")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=64 * 1024)
                shutil.rmtree(destination, ignore_errors=True)
                os.replace(staging, destination)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    except NapCatSetupError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
        raise NapCatSetupError("NAPCAT_ARCHIVE_INVALID") from exc


def prepare_installer(data_root: Path) -> Path:
    if os.name != "nt":
        raise NapCatSetupError("NAPCAT_WINDOWS_REQUIRED")
    root = _root(data_root)
    archive = root / NAPCAT_ASSET
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest().lower() != NAPCAT_SHA256:
        archive.unlink(missing_ok=True)
        _download_archive(archive)
    bootstrap = root / "bootstrap"
    _safe_extract(archive, bootstrap)
    matches = [path for path in bootstrap.rglob("NapCatInstaller.exe") if path.is_file()]
    if len(matches) != 1:
        raise NapCatSetupError("NAPCAT_INSTALLER_NOT_FOUND")
    return matches[0]


def launch_installer(installer: Path) -> subprocess.Popen:
    if os.name != "nt":
        raise NapCatSetupError("NAPCAT_WINDOWS_REQUIRED")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        return subprocess.Popen(
            [str(installer)],
            cwd=str(installer.parent),
            creationflags=flags,
        )
    except OSError as exc:
        raise NapCatSetupError("NAPCAT_INSTALLER_START_FAILED") from exc


def find_shell(data_root: Path) -> Path | None:
    root = _root(data_root)
    candidates: list[Path] = []
    for batch in root.rglob("napcat.bat"):
        try:
            relative = batch.relative_to(root)
        except ValueError:
            continue
        if batch.is_file() and len(relative.parts) <= 6:
            candidates.append(batch.parent)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (len(item.parts), str(item)))[0]


def public_status(data_root: Path, runtime: dict[str, object]) -> dict[str, object]:
    shell = find_shell(data_root)
    task = runtime.get("napcat_task")
    installer = runtime.get("napcat_installer_process")
    shell_process = runtime.get("napcat_shell_process")
    state = str(runtime.get("napcat_state") or "IDLE")
    if shell is not None:
        state = "READY"
    if shell_process is not None and getattr(shell_process, "poll", lambda: 0)() is None:
        state = "RUNNING"
    elif installer is not None and getattr(installer, "poll", lambda: 0)() is None:
        state = "INSTALLER_OPENED"
    elif task is not None and not getattr(task, "done", lambda: True)():
        state = str(runtime.get("napcat_state") or "DOWNLOADING")
    return {
        "state": state,
        "version": NAPCAT_VERSION,
        "source": NAPCAT_SOURCE,
        "installed": shell is not None,
        "managed": True,
    }


def _token_path(data_root: Path) -> Path:
    return _root(data_root) / "onebot-token.dpapi"


def _managed_token(data_root: Path) -> str:
    from original_client_setup_api import _dpapi_protect, _dpapi_unprotect

    path = _token_path(data_root)
    if path.is_file():
        try:
            token = json.loads(_dpapi_unprotect(path.read_text(encoding="utf-8"))).get("token")
        except Exception as exc:
            raise NapCatSetupError("NAPCAT_TOKEN_INVALID") from exc
        if isinstance(token, str) and 32 <= len(token) <= 256:
            return token
        raise NapCatSetupError("NAPCAT_TOKEN_INVALID")
    token = secrets.token_urlsafe(32)
    _atomic_text(path, _dpapi_protect(json.dumps({"token": token})))
    return token


def prepare_onebot(data_root: Path) -> tuple[Path, str]:
    shell = find_shell(data_root)
    if shell is None:
        raise NapCatSetupError("NAPCAT_NOT_INSTALLED")
    token = _managed_token(data_root)
    config = {
        "network": {
            "httpServers": [],
            "httpClients": [],
            "websocketServers": [
                {
                    "name": "Olivia",
                    "enable": True,
                    "host": "127.0.0.1",
                    "port": 3001,
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "token": token,
                    "enableForcePushEvent": True,
                    "debug": False,
                    "heartInterval": 30000,
                }
            ],
            "websocketClients": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }
    _atomic_text(
        shell / "config" / "onebot11.json",
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return shell, token


def launch_shell(data_root: Path) -> subprocess.Popen:
    if os.name != "nt":
        raise NapCatSetupError("NAPCAT_WINDOWS_REQUIRED")
    shell, _token = prepare_onebot(data_root)
    batch = shell / "napcat.bat"
    if not batch.is_file():
        raise NapCatSetupError("NAPCAT_LAUNCHER_NOT_FOUND")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        return subprocess.Popen(
            ["cmd.exe", "/c", str(batch)],
            cwd=str(shell),
            creationflags=flags,
        )
    except OSError as exc:
        raise NapCatSetupError("NAPCAT_START_FAILED") from exc


def managed_connection(data_root: Path) -> tuple[str, str]:
    _shell, token = prepare_onebot(data_root)
    return NAPCAT_WS_URL, token


def open_login_page(data_root: Path) -> bool:
    shell = find_shell(data_root)
    if shell is None:
        return False
    webui = shell / "config" / "webui.json"
    try:
        value = json.loads(webui.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    port = value.get("port")
    token = value.get("token")
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or not token:
        return False
    url = f"http://127.0.0.1:{port}/webui/?token={quote(token, safe='')}"
    return bool(webbrowser.open(url, new=2))


__all__ = [
    "NAPCAT_ASSET",
    "NAPCAT_LICENSE",
    "NAPCAT_SHA256",
    "NAPCAT_SOURCE",
    "NAPCAT_URL",
    "NAPCAT_VERSION",
    "NAPCAT_WS_URL",
    "NapCatSetupError",
    "find_shell",
    "launch_installer",
    "launch_shell",
    "managed_connection",
    "open_login_page",
    "prepare_installer",
    "prepare_onebot",
    "public_status",
]
