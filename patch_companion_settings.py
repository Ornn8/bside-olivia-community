"""Add a bounded companion panel to the supported Olivia settings view.

The patch only changes a staged ``feapp.dat`` archive. It keeps the client main
module byte-for-byte intact, inserts one local bootstrap script into
``index.html``, and rolls the archive back if any validation step fails.
"""

from __future__ import annotations

import hashlib
import html
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import tempfile
from urllib.parse import urlsplit
import zipfile


INDEX_MEMBER = "index.html"
MAIN_MODULE_MEMBER = "assets/main-917d29fc.js"
BOOTSTRAP_MEMBER = "assets/olivia-companion-settings.js"
PATCH_MARKER = "data-olivia-companion-settings"
PATCH_SCHEMA_VERSION = "p03.original-settings-shell.v1"
MAX_ARCHIVE_MEMBERS = 100_000
MAX_TEXT_MEMBER_BYTES = 64 * 1024 * 1024

_MODULE_SCRIPT_RE = re.compile(
    r"<script\b"
    r"(?=[^>]*\btype\s*=\s*([\"'])module\1)"
    r"(?=[^>]*\bsrc\s*=\s*([\"'])\./assets/main-917d29fc\.js\2)"
    r"[^>]*>\s*</script>",
    flags=re.IGNORECASE,
)
_MARKER_TAG_RE = re.compile(
    r"<script\b[^>]*\bdata-olivia-companion-settings="
    r"([\"'])p03\.original-settings-shell\.v1\1[^>]*>",
    flags=re.IGNORECASE,
)
_API_BASE_RE = re.compile(
    r"\bdata-api-base=([\"'])(?P<value>[^\"']+)\1",
    flags=re.IGNORECASE,
)

_BOOTSTRAP_JAVASCRIPT = r'''(() => {
  "use strict";

  const loader = document.currentScript;
  const rawApiBase = loader && loader.dataset ? loader.dataset.apiBase : "";
  const ROOT_ATTR = "data-olivia-companion-settings-root";
  const DIALOG_ATTR = "data-olivia-companion-settings-dialog";
  const STATUS_PATH = "/toy/companion/status";

  const parseApiBase = (value) => {
    let url;
    try {
      url = new URL(value);
    } catch (_error) {
      return null;
    }
    const loopback = url.hostname === "127.0.0.1" || url.hostname === "localhost";
    if (
      url.protocol !== "http:" ||
      !loopback ||
      !url.port ||
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      (url.pathname !== "/" && url.pathname !== "")
    ) {
      return null;
    }
    return url;
  };

  const apiBase = parseApiBase(rawApiBase);
  if (!apiBase) {
    return;
  }

  const text = (tag, value, className) => {
    const element = document.createElement(tag);
    element.textContent = value;
    if (className) {
      element.className = className;
    }
    return element;
  };

  const button = (label, onClick) => {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    element.className = "px-6 py-2.5 rounded-full border border-grey-5 text-text-body text-label-m font-medium cursor-pointer hover:bg-surface-1 transition-colors";
    element.addEventListener("click", onClick);
    return element;
  };

  const isSettingsRoute = () => {
    const route = `${window.location.pathname} ${window.location.hash}`;
    return /(?:^|[\/#])settings(?:[\/?#]|$)/i.test(route);
  };

  const removeShell = () => {
    document.querySelector(`[${ROOT_ATTR}]`)?.remove();
    document.querySelector(`[${DIALOG_ATTR}]`)?.remove();
  };

  const statusMessage = (node, value) => {
    node.textContent = value;
    node.dataset.state = value === "本机陪伴服务可用。" ? "available" : "unavailable";
  };

  const loadStatus = async (node) => {
    statusMessage(node, "正在连接本机陪伴服务……");
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 3000);
    try {
      const endpoint = new URL(STATUS_PATH, apiBase);
      const response = await fetch(endpoint, {
        method: "GET",
        cache: "no-store",
        credentials: "omit",
        headers: { "Accept": "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error("unavailable");
      }
      const payload = await response.json();
      if (!payload || payload.status !== "READY") {
        throw new Error("unavailable");
      }
      statusMessage(node, "本机陪伴服务可用。");
    } catch (_error) {
      statusMessage(node, "本机陪伴服务暂不可用。");
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const openDialog = () => {
    document.querySelector(`[${DIALOG_ATTR}]`)?.remove();

    const backdrop = document.createElement("div");
    backdrop.setAttribute(DIALOG_ATTR, "");
    backdrop.style.position = "fixed";
    backdrop.style.inset = "0";
    backdrop.style.zIndex = "2147483000";
    backdrop.style.display = "grid";
    backdrop.style.placeItems = "center";
    backdrop.style.padding = "40px";
    backdrop.style.background = "rgba(0, 0, 0, 0.62)";

    const dialog = document.createElement("section");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "olivia-companion-dialog-title");
    dialog.style.width = "min(760px, calc(100vw - 80px))";
    dialog.style.maxHeight = "calc(100vh - 80px)";
    dialog.style.overflow = "auto";
    dialog.style.borderRadius = "16px";
    dialog.style.padding = "28px";
    dialog.style.background = "var(--el-bg-color, #202124)";
    dialog.style.boxShadow = "0 24px 80px rgba(0, 0, 0, 0.45)";

    const header = document.createElement("div");
    header.style.display = "flex";
    header.style.alignItems = "center";
    header.style.justifyContent = "space-between";
    header.style.gap = "24px";

    const heading = text("h2", "本地陪伴", "text-text-title text-headline-m");
    heading.id = "olivia-companion-dialog-title";
    heading.style.margin = "0";

    const close = button("关闭", () => backdrop.remove());
    header.append(heading, close);

    const status = text(
      "p",
      "正在连接本机陪伴服务……",
      "text-text-secondary text-body-m font-regular"
    );
    status.setAttribute("aria-live", "polite");
    status.style.margin = "20px 0";

    const tabs = document.createElement("div");
    tabs.setAttribute("role", "tablist");
    tabs.style.display = "flex";
    tabs.style.gap = "12px";
    tabs.style.marginBottom = "18px";

    const panels = document.createElement("div");
    const definitions = [
      {
        id: "memory",
        label: "长期记忆",
        description: "查看、纠正或删除林离在新对话中形成的本地记忆。",
      },
      {
        id: "private-world",
        label: "私人世界",
        description: "管理关系边界、私人称呼、住所权限和本地世界线。",
      },
    ];

    const showPanel = (id) => {
      for (const tab of tabs.querySelectorAll('[role="tab"]')) {
        const active = tab.dataset.panelId === id;
        tab.setAttribute("aria-selected", active ? "true" : "false");
        tab.style.background = active ? "rgba(255,255,255,0.12)" : "transparent";
      }
      for (const panel of panels.querySelectorAll('[role="tabpanel"]')) {
        panel.hidden = panel.dataset.panelId !== id;
      }
    };

    for (const definition of definitions) {
      const tab = button(definition.label, () => showPanel(definition.id));
      tab.setAttribute("role", "tab");
      tab.dataset.panelId = definition.id;
      tab.setAttribute("aria-controls", `olivia-companion-panel-${definition.id}`);
      tabs.append(tab);

      const panel = document.createElement("section");
      panel.id = `olivia-companion-panel-${definition.id}`;
      panel.dataset.panelId = definition.id;
      panel.dataset.oliviaCompanionPanel = definition.id;
      panel.setAttribute("role", "tabpanel");
      panel.style.padding = "18px";
      panel.style.borderRadius = "12px";
      panel.style.background = "rgba(255,255,255,0.06)";
      panel.append(
        text("h3", definition.label, "text-text-title text-title-m"),
        text("p", definition.description, "text-text-secondary text-body-m font-regular")
      );
      panels.append(panel);
    }

    dialog.append(header, status, tabs, panels);
    backdrop.append(dialog);
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) {
        backdrop.remove();
      }
    });
    backdrop.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        backdrop.remove();
      }
    });
    document.body.append(backdrop);
    showPanel("memory");
    close.focus();
    loadStatus(status);
  };

  const findSettingsContainer = () => {
    for (const main of document.querySelectorAll("main")) {
      const sections = main.querySelectorAll(".tp-settings-item");
      if (sections.length) {
        return sections[sections.length - 1].parentElement;
      }
    }
    return null;
  };

  const mountShell = () => {
    if (!isSettingsRoute()) {
      removeShell();
      return;
    }
    if (document.querySelector(`[${ROOT_ATTR}]`)) {
      return;
    }
    const container = findSettingsContainer();
    if (!container) {
      return;
    }

    const section = document.createElement("div");
    section.setAttribute(ROOT_ATTR, "");
    section.className = "tp-settings-item";

    const title = text("div", "本地陪伴", "text-text-body text-title-m");
    const row = document.createElement("div");
    row.className = "flex items-center justify-between px-0 py-3 rounded-3";

    const copy = document.createElement("div");
    copy.className = "flex flex-col gap-0 flex-1 min-w-0";
    copy.append(
      text("div", "记忆与私人世界", "text-text-body text-label-l"),
      text(
        "div",
        "在 Olivia 客户端内管理本地连续性。",
        "text-text-secondary text-body-m font-regular"
      )
    );

    row.append(copy, button("打开", openDialog));
    section.append(title, row);
    container.append(section);
  };

  let scheduled = false;
  const schedule = () => {
    if (scheduled) {
      return;
    }
    scheduled = true;
    window.requestAnimationFrame(() => {
      scheduled = false;
      mountShell();
    });
  };

  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", schedule);
  window.addEventListener("popstate", schedule);
  schedule();
})();
'''


class CompanionSettingsPatchError(RuntimeError):
    """Stable archive patch failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNREADABLE") from exc
    return digest.hexdigest()


def validate_api_base(value: str | None) -> str:
    if not value:
        raise CompanionSettingsPatchError("COMPANION_API_BASE_REQUIRED")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CompanionSettingsPatchError("COMPANION_API_BASE_INVALID") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CompanionSettingsPatchError("COMPANION_API_BASE_INVALID")
    return f"http://{parsed.hostname}:{port}/"


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
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNSAFE")
    target = (root / Path(*posix.parts)).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNSAFE")
    return target


def _validate_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members:
                raise CompanionSettingsPatchError("COMPANION_ARCHIVE_EMPTY")
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise CompanionSettingsPatchError("COMPANION_ARCHIVE_TOO_MANY_MEMBERS")
            for info in members:
                _safe_member_path(path.parent.resolve(), info.filename)
    except CompanionSettingsPatchError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_INVALID") from exc


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
            raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNREADABLE") from exc


def _member_hashes(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                result[info.filename] = hashlib.sha256(archive.read(info)).hexdigest()
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_UNREADABLE") from exc
    return result


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    except OSError as exc:
        raise CompanionSettingsPatchError("COMPANION_BACKUP_FAILED") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _ensure_backup(feapp: Path) -> Path:
    backup = Path(str(feapp) + ".companion.orig")
    if backup.exists():
        _validate_archive(backup)
    else:
        _atomic_copy(feapp, backup)
    return backup


def _read_text(path: Path, code: str) -> str:
    try:
        if path.stat().st_size > MAX_TEXT_MEMBER_BYTES:
            raise CompanionSettingsPatchError(code)
        return path.read_text(encoding="utf-8")
    except CompanionSettingsPatchError:
        raise
    except (OSError, UnicodeError) as exc:
        raise CompanionSettingsPatchError(code) from exc


def _patch_index(root: Path, api_base: str) -> str:
    index = root / INDEX_MEMBER
    main_module = root / MAIN_MODULE_MEMBER
    bootstrap = root / BOOTSTRAP_MEMBER
    if not index.is_file():
        raise CompanionSettingsPatchError("COMPANION_INDEX_MISSING")
    if not main_module.is_file():
        raise CompanionSettingsPatchError("COMPANION_MAIN_MODULE_MISSING")
    source = _read_text(index, "COMPANION_INDEX_UNREADABLE")

    marker_count = source.count(PATCH_MARKER)
    if marker_count == 1:
        if not bootstrap.is_file():
            raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")
        tag = _MARKER_TAG_RE.search(source)
        api_match = _API_BASE_RE.search(tag.group(0) if tag else "")
        if not tag or not api_match:
            raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")
        if html.unescape(api_match.group("value")) != api_base:
            raise CompanionSettingsPatchError("COMPANION_API_BASE_MISMATCH")
        if _read_text(bootstrap, "COMPANION_BOOTSTRAP_UNREADABLE") != _BOOTSTRAP_JAVASCRIPT:
            raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")
        return "ALREADY_PATCHED"
    if marker_count or bootstrap.exists():
        raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")

    matches = list(_MODULE_SCRIPT_RE.finditer(source))
    if len(matches) != 1:
        raise CompanionSettingsPatchError("COMPANION_MODULE_ANCHOR_INVALID")
    match = matches[0]
    tag = (
        '<script src="./assets/olivia-companion-settings.js" '
        f'{PATCH_MARKER}="{PATCH_SCHEMA_VERSION}" '
        f'data-api-base="{html.escape(api_base, quote=True)}"></script>'
    )
    patched = source[: match.end()] + "\n  " + tag + source[match.end() :]
    if patched.count(PATCH_MARKER) != 1:
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    try:
        index.write_text(patched, encoding="utf-8")
        bootstrap.write_text(_BOOTSTRAP_JAVASCRIPT, encoding="utf-8")
    except OSError as exc:
        raise CompanionSettingsPatchError("COMPANION_PATCH_WRITE_FAILED") from exc
    return "PATCHED"


def _repack(root: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        _validate_archive(temporary)
        os.replace(temporary, destination)
    except CompanionSettingsPatchError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CompanionSettingsPatchError("COMPANION_REPACK_FAILED") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _verify_archive(
    path: Path,
    *,
    api_base: str,
    original_hashes: dict[str, str],
) -> None:
    patched_hashes = _member_hashes(path)
    if set(patched_hashes) != set(original_hashes) | {BOOTSTRAP_MEMBER}:
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")
    for name, digest in original_hashes.items():
        if name == INDEX_MEMBER:
            continue
        if patched_hashes.get(name) != digest:
            raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")
    try:
        with zipfile.ZipFile(path) as archive:
            index = archive.read(INDEX_MEMBER).decode("utf-8")
            bootstrap = archive.read(BOOTSTRAP_MEMBER).decode("utf-8")
    except (KeyError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED") from exc
    required = (
        'data-olivia-companion-settings="p03.original-settings-shell.v1"',
        f'data-api-base="{html.escape(api_base, quote=True)}"',
    )
    bootstrap_required = (
        'const STATUS_PATH = "/toy/companion/status";',
        'data-olivia-companion-settings-root',
        'panel.dataset.oliviaCompanionPanel',
        '长期记忆',
        '私人世界',
        'new MutationObserver',
    )
    if (
        any(value not in index for value in required)
        or any(value not in bootstrap for value in bootstrap_required)
        or "<iframe" in bootstrap.casefold()
        or "window.open" in bootstrap
    ):
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")


def patch_companion_settings(
    feapp_path: str | os.PathLike[str],
    api_base: str | None,
    *,
    work_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Patch one staged client archive and preserve every existing asset."""

    feapp = Path(feapp_path).expanduser().resolve()
    normalized_api_base = validate_api_base(api_base)
    if not feapp.is_file():
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_NOT_FOUND")
    _validate_archive(feapp)
    original_hashes = _member_hashes(feapp)
    source_sha256 = sha256_file(feapp)
    backup = _ensure_backup(feapp)
    backup_sha256 = sha256_file(backup)
    sandbox = Path(work_root or feapp.parent).expanduser().resolve()
    if not sandbox.is_dir():
        raise CompanionSettingsPatchError("COMPANION_WORK_ROOT_NOT_FOUND")

    with tempfile.TemporaryDirectory(prefix=".patch-companion-settings-", dir=sandbox) as name:
        temporary_root = Path(name)
        rollback = temporary_root / "rollback.dat"
        _atomic_copy(feapp, rollback)
        try:
            unpacked = temporary_root / "unpacked"
            with zipfile.ZipFile(feapp) as archive:
                _safe_extract(archive, unpacked)
            status = _patch_index(unpacked, normalized_api_base)
            if status == "PATCHED":
                output = temporary_root / "patched.dat"
                _repack(unpacked, output)
                os.replace(output, feapp)
            _verify_archive(
                feapp,
                api_base=normalized_api_base,
                original_hashes=original_hashes,
            )
        except Exception:
            _atomic_copy(rollback, feapp)
            raise

    return {
        "schema_version": PATCH_SCHEMA_VERSION,
        "status": status,
        "source_sha256": source_sha256,
        "backup_sha256": backup_sha256,
        "patched_sha256": sha256_file(feapp),
        "backup_name": backup.name,
    }


__all__ = [
    "BOOTSTRAP_MEMBER",
    "CompanionSettingsPatchError",
    "INDEX_MEMBER",
    "MAIN_MODULE_MEMBER",
    "PATCH_MARKER",
    "PATCH_SCHEMA_VERSION",
    "patch_companion_settings",
    "sha256_file",
    "validate_api_base",
]
