"""Safely patch the local frontend archive without touching it on failure.

The CLI requires an explicit localhost WebSocket target. It never contacts a
server and its tests use only temporary archives.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit


MAIN_JS = "assets/main-917d29fc.js"
HE_ANCHOR = "He=e=>new Promise((t,n)=>{try{"
INJECT_ANCHOR = ',"query.response":no(a)}}),t(c)},onFailure:'
MAILBOX_LOGIN_ANCHOR = (
    '!z.isNew||N?(await t.replace({name:ye.Home}),'
    'await h(z.uid.toString(),z.modelGatewayToken||"",!1))'
)
MAILBOX_LOGIN_REPLACEMENT = (
    '!z.isNew||N?(localStorage.setItem("appMode","lite"),'
    'await t.replace({name:ye.Collection}),'
    'await h(z.uid.toString(),z.modelGatewayToken||"",!1))'
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_ws_target(new_ws: str | None) -> str:
    if not new_ws:
        raise ValueError("an explicit localhost WebSocket target is required")
    parsed = urlsplit(new_ws)
    if parsed.scheme != "ws" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("WebSocket target must use ws://localhost or ws://127.0.0.1")
    if parsed.port is None or not 1 <= parsed.port <= 65535:
        raise ValueError("WebSocket target must include a valid port")
    return new_ws


def _safe_member_path(root: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if not normalized or "\x00" in normalized or posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError(f"unsafe archive member: {member_name!r}")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError(f"unsafe archive member: {member_name!r}")
    target = (root / Path(*posix.parts)).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise ValueError(f"archive member escapes temporary root: {member_name!r}")
    return target


def _safe_extract(archive: zipfile.ZipFile, root: Path) -> None:
    for info in archive.infolist():
        target = _safe_member_path(root, info.filename)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if not archive.namelist():
                raise ValueError("archive has no entries")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("input or backup archive is invalid") from exc


def _patch_mailbox_default_route(javascript: str) -> str:
    if javascript.count(MAILBOX_LOGIN_ANCHOR) != 1:
        raise ValueError("fresh lite login mailbox anchor is missing or not unique")
    return javascript.replace(MAILBOX_LOGIN_ANCHOR, MAILBOX_LOGIN_REPLACEMENT, 1)


def _ensure_backup(feapp: Path) -> Path:
    backup = Path(str(feapp) + ".orig")
    if backup.exists():
        _validate_zip(backup)
    else:
        _atomic_copy(feapp, backup)
    return backup


def _repack(source_root: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source_root).as_posix())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def patch_feapp(feapp_path: str | os.PathLike[str], new_ws: str | None,
                work_root: str | os.PathLike[str] | None = None) -> dict[str, str]:
    feapp = Path(feapp_path).resolve()
    validate_ws_target(new_ws)
    parsed_ws = urlsplit(new_ws)
    new_http = f"http://{parsed_ws.netloc}"
    if not feapp.is_file():
        raise FileNotFoundError(feapp)
    _validate_zip(feapp)
    source_sha256 = sha256_file(feapp)
    backup = _ensure_backup(feapp)
    backup_sha256 = sha256_file(backup)
    sandbox = Path(work_root or feapp.parent).resolve()
    if not sandbox.is_dir():
        raise FileNotFoundError(sandbox)

    with tempfile.TemporaryDirectory(prefix=".patch-feapp-", dir=sandbox) as temp_name:
        temporary_root = Path(temp_name)
        rollback = temporary_root / "rollback.dat"
        _atomic_copy(feapp, rollback)
        try:
            with zipfile.ZipFile(feapp) as archive:
                _safe_extract(archive, temporary_root / "unpacked")
            root = temporary_root / "unpacked"
            main_path = root / Path(*MAIN_JS.split("/"))
            if not main_path.is_file():
                raise ValueError(f"missing {MAIN_JS}")
            javascript = main_path.read_text(encoding="utf-8")
            if javascript.count(HE_ANCHOR) != 1:
                raise ValueError("He anchor is missing or not unique")
            index = javascript.find(INJECT_ANCHOR)
            if index < 0:
                raise ValueError("onSuccess injection anchor is missing")
            patch = (
                'c&&e.action==="getClientConfig"&&c&&c.appConf&&('
                'c.appConf.toyWsUrl=' + json.dumps(new_ws) + ','
                + 'c.appConf.toyApiUrl=' + json.dumps(new_http)
                + ')'
            )
            head_end = index + len(',"query.response":no(a)}}),')
            main_path.write_text(javascript[:head_end] + patch + ',' + javascript[head_end:], encoding="utf-8")
            patched_javascript = main_path.read_text(encoding="utf-8")
            main_path.write_text(
                _patch_mailbox_default_route(patched_javascript), encoding="utf-8"
            )
            output_archive = temporary_root / "patched.dat"
            _repack(root, output_archive)
            _validate_zip(output_archive)
            os.replace(output_archive, feapp)
            with zipfile.ZipFile(feapp) as archive:
                patched_js = archive.read(MAIN_JS).decode("utf-8")
            if (
                "toyApiUrl" not in patched_js
                or new_http not in patched_js
                or "toyWsUrl" not in patched_js
                or new_ws not in patched_js
                or 'localStorage.setItem("appMode","lite")' not in patched_js
                or "await t.replace({name:ye.Collection})" not in patched_js
            ):
                raise ValueError("patched archive verification failed")
        except Exception:
            _atomic_copy(rollback, feapp)
            raise

    return {
        "source_sha256": source_sha256,
        "backup_sha256": backup_sha256,
        "patched_sha256": sha256_file(feapp),
        "backup_path": str(backup),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feapp", nargs="?", default=r"0.0.9.615\resources\feapp.dat")
    parser.add_argument("new_ws", help="explicit ws://localhost:<port>/... target")
    args = parser.parse_args()
    result = patch_feapp(args.feapp, args.new_ws)
    print(json.dumps({"status": "PATCHED", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
