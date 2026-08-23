"""Safely add a loopback-only local-media fallback to original ``webplayer.dat``.

The supported original client already opens ``webplayer`` with ``uid``,
``volume`` and ``muted`` query parameters.  This patch does not alter that
normal path.  It replaces the original module script tag with a small local
bootstrap asset.  The bootstrap loads the untouched original module unless
``uid`` is an explicitly allowed loopback media URL.

The patch is deliberately not installed by this module.  Installer integration
and the backend media contract are separate reviewable changes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import tempfile
import zipfile


INDEX_MEMBER = "index.html"
BOOTSTRAP_MEMBER = "assets/olivia-local-media-bootstrap.js"
PATCH_MARKER = "data-olivia-local-media-bootstrap"
PATCH_SCHEMA_VERSION = "p03.webplayer-local-media.v1"
MAX_ARCHIVE_MEMBERS = 100_000
MAX_TEXT_MEMBER_BYTES = 64 * 1024 * 1024

_MODULE_SCRIPT_RE = re.compile(
    r"<script\b"
    r"(?=[^>]*\btype\s*=\s*([\"'])module\1)"
    r"(?=[^>]*\bsrc\s*=\s*([\"'])(?P<src>[^\"']+\.js(?:\?[^\"']*)?)\2)"
    r"[^>]*>\s*</script>",
    flags=re.IGNORECASE,
)

_BOOTSTRAP_JAVASCRIPT = r'''(() => {
  "use strict";

  const script = document.currentScript;
  const originalModule = script && script.dataset
    ? script.dataset.originalModule
    : "";

  const loadOriginal = () => {
    if (!originalModule) {
      return;
    }
    const moduleScript = document.createElement("script");
    moduleScript.type = "module";
    moduleScript.src = originalModule;
    document.head.appendChild(moduleScript);
  };

  const parseLocalMedia = () => {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get("uid");
    if (!raw) {
      return null;
    }

    let url;
    try {
      url = new URL(raw);
    } catch (_error) {
      return null;
    }

    const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost";
    const mediaPath = url.pathname.startsWith("/toy/media/") ||
      url.pathname.startsWith("/media/");
    if (
      url.protocol !== "http:" ||
      !loopback ||
      !url.port ||
      url.username ||
      url.password ||
      url.hash ||
      !mediaPath
    ) {
      return null;
    }

    const volumeValue = Number(params.get("volume"));
    const volume = Number.isFinite(volumeValue)
      ? Math.min(1, Math.max(0, volumeValue))
      : 1;
    const mutedValue = (params.get("muted") || "").toLowerCase();
    const muted = mutedValue === "1" || mutedValue === "true";
    return { url: url.href, volume, muted };
  };

  const localMedia = parseLocalMedia();
  if (!localMedia) {
    loadOriginal();
    return;
  }

  Object.defineProperty(window, "__OLIVIA_LOCAL_MEDIA_ACTIVE__", {
    value: true,
    configurable: false,
    enumerable: false,
    writable: false,
  });

  const render = () => {
    const root = document.createElement("main");
    root.setAttribute("data-olivia-local-media-root", "");
    root.style.position = "fixed";
    root.style.inset = "0";
    root.style.display = "grid";
    root.style.placeItems = "center";
    root.style.background = "#000";

    const video = document.createElement("video");
    video.setAttribute("data-olivia-local-media-video", "");
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    video.preload = "auto";
    video.muted = localMedia.muted;
    video.volume = localMedia.volume;
    video.style.width = "100%";
    video.style.height = "100%";
    video.style.objectFit = "contain";
    video.src = localMedia.url;

    const failure = document.createElement("p");
    failure.hidden = true;
    failure.textContent = "本地视频暂时无法播放。";
    failure.style.color = "#fff";
    failure.style.fontFamily = "system-ui, sans-serif";

    video.addEventListener("error", () => {
      video.hidden = true;
      failure.hidden = false;
    }, { once: true });

    root.append(video, failure);
    document.body.replaceChildren(root);
    document.documentElement.style.background = "#000";
    document.body.style.margin = "0";
    document.body.style.overflow = "hidden";
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render, { once: true });
  } else {
    render();
  }
})();
'''


class WebPlayerPatchError(RuntimeError):
    """Stable webplayer patch failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise WebPlayerPatchError("WEBPLAYER_ARCHIVE_UNREADABLE") from exc
    return digest.hexdigest()


def _safe_member_path(root: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise WebPlayerPatchError("WEBPLAYER_ARCHIVE_UNSAFE")
    target = (root / Path(*posix.parts)).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise WebPlayerPatchError("WEBPLAYER_ARCHIVE_UNSAFE")
    return target


def _validate_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members:
                raise WebPlayerPatchError("WEBPLAYER_ARCHIVE_EMPTY")
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise WebPlayerPatchError("WEBPLAYER_ARCHIVE_TOO_MANY_MEMBERS")
            for info in members:
                _safe_member_path(Path(path.parent).resolve(), info.filename)
    except WebPlayerPatchError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise WebPlayerPatchError("WEBPLAYER_ARCHIVE_INVALID") from exc


def _safe_extract(archive: zipfile.ZipFile, root: Path) -> None:
    for info in archive.infolist():
        target = _safe_member_path(root, info.filename)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        except OSError as exc:
            raise WebPlayerPatchError("WEBPLAYER_ARCHIVE_UNREADABLE") from exc


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    except OSError as exc:
        raise WebPlayerPatchError("WEBPLAYER_BACKUP_FAILED") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _ensure_backup(webplayer: Path) -> Path:
    backup = Path(str(webplayer) + ".orig")
    if backup.exists():
        _validate_archive(backup)
    else:
        _atomic_copy(webplayer, backup)
    return backup


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_TEXT_MEMBER_BYTES:
            raise WebPlayerPatchError("WEBPLAYER_INDEX_TOO_LARGE")
        return path.read_text(encoding="utf-8")
    except WebPlayerPatchError:
        raise
    except (OSError, UnicodeError) as exc:
        raise WebPlayerPatchError("WEBPLAYER_INDEX_UNREADABLE") from exc


def _resolve_original_module(root: Path, src: str) -> Path:
    clean = src.split("?", 1)[0].replace("\\", "/")
    if clean.startswith("./"):
        clean = clean[2:]
    module = _safe_member_path(root, clean)
    if not module.is_file() or module.suffix.casefold() != ".js":
        raise WebPlayerPatchError("WEBPLAYER_MODULE_MISSING")
    return module


def _patch_index(root: Path) -> str:
    index = root / INDEX_MEMBER
    if not index.is_file():
        raise WebPlayerPatchError("WEBPLAYER_INDEX_MISSING")
    html = _read_text(index)

    if html.count(PATCH_MARKER) == 1:
        if not (root / BOOTSTRAP_MEMBER).is_file():
            raise WebPlayerPatchError("WEBPLAYER_PATCH_INCOMPLETE")
        return "ALREADY_PATCHED"
    if PATCH_MARKER in html or (root / BOOTSTRAP_MEMBER).exists():
        raise WebPlayerPatchError("WEBPLAYER_PATCH_INCOMPLETE")

    matches = list(_MODULE_SCRIPT_RE.finditer(html))
    if len(matches) != 1:
        raise WebPlayerPatchError("WEBPLAYER_MODULE_ANCHOR_INVALID")
    match = matches[0]
    original_src = match.group("src")
    _resolve_original_module(root, original_src)

    replacement = (
        '<script src="./assets/olivia-local-media-bootstrap.js" '
        f'{PATCH_MARKER}="{PATCH_SCHEMA_VERSION}" '
        f'data-original-module={json.dumps(original_src)}></script>'
    )
    patched = html[: match.start()] + replacement + html[match.end() :]
    if patched.count(PATCH_MARKER) != 1 or original_src not in patched:
        raise WebPlayerPatchError("WEBPLAYER_PATCH_VERIFICATION_FAILED")

    bootstrap = root / BOOTSTRAP_MEMBER
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    try:
        index.write_text(patched, encoding="utf-8")
        bootstrap.write_text(_BOOTSTRAP_JAVASCRIPT, encoding="utf-8")
    except OSError as exc:
        raise WebPlayerPatchError("WEBPLAYER_PATCH_WRITE_FAILED") from exc
    return "PATCHED"


def _repack(source_root: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source_root).as_posix())
        _validate_archive(temporary)
        os.replace(temporary, destination)
    except WebPlayerPatchError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise WebPlayerPatchError("WEBPLAYER_REPACK_FAILED") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _verify_patched_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            html = archive.read(INDEX_MEMBER).decode("utf-8")
            bootstrap = archive.read(BOOTSTRAP_MEMBER).decode("utf-8")
    except (KeyError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise WebPlayerPatchError("WEBPLAYER_PATCH_VERIFICATION_FAILED") from exc
    required_bootstrap_tokens = (
        'params.get("uid")',
        'url.hostname === "127.0.0.1"',
        'url.hostname === "localhost"',
        'url.pathname.startsWith("/toy/media/")',
        'url.pathname.startsWith("/media/")',
        "loadOriginal();",
        'document.createElement("video")',
    )
    if (
        html.count(PATCH_MARKER) != 1
        or BOOTSTRAP_MEMBER.split("/", 1)[1] not in html
        or any(token not in bootstrap for token in required_bootstrap_tokens)
    ):
        raise WebPlayerPatchError("WEBPLAYER_PATCH_VERIFICATION_FAILED")


def patch_webplayer(
    webplayer_path: str | os.PathLike[str],
    *,
    work_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Patch one supported original player archive with rollback on failure."""

    webplayer = Path(webplayer_path).expanduser().resolve()
    if not webplayer.is_file():
        raise WebPlayerPatchError("WEBPLAYER_ARCHIVE_NOT_FOUND")
    _validate_archive(webplayer)
    source_sha256 = sha256_file(webplayer)
    backup = _ensure_backup(webplayer)
    backup_sha256 = sha256_file(backup)
    sandbox = Path(work_root or webplayer.parent).expanduser().resolve()
    if not sandbox.is_dir():
        raise WebPlayerPatchError("WEBPLAYER_WORK_ROOT_NOT_FOUND")

    with tempfile.TemporaryDirectory(prefix=".patch-webplayer-", dir=sandbox) as name:
        temporary_root = Path(name)
        rollback = temporary_root / "rollback.dat"
        _atomic_copy(webplayer, rollback)
        try:
            extracted = temporary_root / "unpacked"
            with zipfile.ZipFile(webplayer) as archive:
                _safe_extract(archive, extracted)
            status = _patch_index(extracted)
            if status == "PATCHED":
                output = temporary_root / "patched.dat"
                _repack(extracted, output)
                os.replace(output, webplayer)
            _verify_patched_archive(webplayer)
        except Exception:
            _atomic_copy(rollback, webplayer)
            raise

    return {
        "schema_version": PATCH_SCHEMA_VERSION,
        "status": status,
        "source_sha256": source_sha256,
        "backup_sha256": backup_sha256,
        "patched_sha256": sha256_file(webplayer),
        "backup_name": backup.name,
    }


__all__ = [
    "BOOTSTRAP_MEMBER",
    "INDEX_MEMBER",
    "PATCH_MARKER",
    "PATCH_SCHEMA_VERSION",
    "WebPlayerPatchError",
    "patch_webplayer",
    "sha256_file",
]
