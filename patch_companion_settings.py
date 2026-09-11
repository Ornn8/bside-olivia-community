"""Add a bounded companion panel to the supported Olivia settings view.

The patch only changes a staged ``feapp.dat`` archive. It inserts one
repository-owned local script into ``index.html`` and, for the supported
0.0.9.627 bundle, repairs known mailbox write-visibility and initial-quota anchors.
Every other existing member stays byte-for-byte intact, and any validation
failure rolls the archive back.
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

from original_client_settings_ui import (
    BOOTSTRAP_JAVASCRIPT,
    SETTINGS_UI_VERSION,
)
from patch_feapp import (
    MAILBOX_WRITE_ANCHOR_0627,
    MAILBOX_WRITE_REPLACEMENT_0627,
    MAIN_JS_0627,
    _repair_mailbox_waiting_footer,
)


INDEX_MEMBER = "index.html"
MAIN_MODULE_MEMBER = "assets/main-917d29fc.js"
MAIN_MODULE_MEMBERS = (
    MAIN_MODULE_MEMBER,
    "assets/main-31595bd3.js",
)
BOOTSTRAP_MEMBER = "assets/olivia-companion-settings.js"
PATCH_MARKER = "data-olivia-companion-settings"
PATCH_SCHEMA_VERSION = "p03.original-settings-shell.v1"
MAX_ARCHIVE_MEMBERS = 100_000
MAX_TEXT_MEMBER_BYTES = 64 * 1024 * 1024

_MODULE_SCRIPT_RE = re.compile(
    r"<script\b"
    r"(?=[^>]*\btype\s*=\s*([\"'])module\1)"
    r"(?=[^>]*\bsrc\s*=\s*([\"'])\./assets/"
    r"(?P<main>main-(?:917d29fc|31595bd3)\.js)\2)"
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
_BOOTSTRAP_SOURCE_RE = re.compile(
    r"\bsrc=([\"'])\./assets/olivia-companion-settings\.js\1",
    flags=re.IGNORECASE,
)
_UI_VERSION_RE = re.compile(
    r"\bdata-ui-version=([\"'])(?P<value>[^\"']+)\1",
    flags=re.IGNORECASE,
)


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


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _write_utf8(path: Path, value: str) -> None:
    try:
        path.write_bytes(value.encode("utf-8"))
    except OSError as exc:
        raise CompanionSettingsPatchError("COMPANION_PATCH_WRITE_FAILED") from exc


def _managed_tag(api_base: str) -> str:
    return (
        '<script src="./assets/olivia-companion-settings.js" '
        f'{PATCH_MARKER}="{PATCH_SCHEMA_VERSION}" '
        f'data-ui-version="{SETTINGS_UI_VERSION}" '
        f'data-api-base="{html.escape(api_base, quote=True)}"></script>'
    )


def _patch_existing(
    source: str,
    bootstrap: Path,
    api_base: str,
) -> str:
    if not bootstrap.is_file():
        raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")
    tag = _MARKER_TAG_RE.search(source)
    api_match = _API_BASE_RE.search(tag.group(0) if tag else "")
    source_match = _BOOTSTRAP_SOURCE_RE.search(tag.group(0) if tag else "")
    if not tag or not api_match or not source_match:
        raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")
    if html.unescape(api_match.group("value")) != api_base:
        raise CompanionSettingsPatchError("COMPANION_API_BASE_MISMATCH")

    current_script = _read_text(
        bootstrap,
        "COMPANION_BOOTSTRAP_UNREADABLE",
    )
    ui_match = _UI_VERSION_RE.search(tag.group(0))
    if (
        _normalize_newlines(current_script)
        == _normalize_newlines(BOOTSTRAP_JAVASCRIPT)
        and ui_match
        and html.unescape(ui_match.group("value")) == SETTINGS_UI_VERSION
    ):
        return "ALREADY_PATCHED"

    managed = _managed_tag(api_base)
    updated = source[: tag.start()] + managed + source[tag.end() :]
    index = bootstrap.parent.parent / INDEX_MEMBER
    _write_utf8(index, updated)
    _write_utf8(bootstrap, BOOTSTRAP_JAVASCRIPT)
    return "PATCHED"


def _patch_index(root: Path, api_base: str) -> str:
    index = root / INDEX_MEMBER
    bootstrap = root / BOOTSTRAP_MEMBER
    if not index.is_file():
        raise CompanionSettingsPatchError("COMPANION_INDEX_MISSING")
    source = _read_text(index, "COMPANION_INDEX_UNREADABLE")

    marker_count = source.count(PATCH_MARKER)
    if marker_count == 1:
        return _patch_existing(source, bootstrap, api_base)
    if marker_count or bootstrap.exists():
        raise CompanionSettingsPatchError("COMPANION_PATCH_INCOMPLETE")

    matches = list(_MODULE_SCRIPT_RE.finditer(source))
    if len(matches) != 1:
        raise CompanionSettingsPatchError("COMPANION_MODULE_ANCHOR_INVALID")
    match = matches[0]
    main_member = f"assets/{match.group('main')}"
    if main_member not in MAIN_MODULE_MEMBERS or not (root / main_member).is_file():
        raise CompanionSettingsPatchError("COMPANION_MAIN_MODULE_MISSING")
    patched = (
        source[: match.end()]
        + "\n  "
        + _managed_tag(api_base)
        + source[match.end() :]
    )
    if patched.count(PATCH_MARKER) != 1:
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    _write_utf8(index, patched)
    _write_utf8(bootstrap, BOOTSTRAP_JAVASCRIPT)
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


_LOCAL_MAILBOX_REQUEST_HELPER = (
    'function oliviaLocalMailboxRequest(e){try{'
    'const s=document.querySelector("script[data-olivia-companion-settings][data-api-base]");'
    'if(!s)return false;const b=new URL(s.dataset.apiBase),u=new URL(e.url,e.baseURL);'
    'return b.protocol==="http:"&&["127.0.0.1","localhost"].includes(b.hostname)'
    '&&u.origin===b.origin&&!u.username&&!u.password'
    '&&/^\\/(?:toy\\/)?letter\\/(?:list|detail|unread_count|send|resend)$/.test(u.pathname)'
    '}catch{return false}}'
)


def _repair_mailbox_write_access(root: Path) -> str:
    main = root / Path(*MAIN_JS_0627.split("/"))
    if not main.is_file():
        return "UNCHANGED"
    source = _read_text(main, "COMPANION_MAIN_MODULE_UNREADABLE")
    # The offline server grants 99 current-mailbox writes.  The native store
    # otherwise initializes/resets to zero until its account-driven refresh,
    # leaving a fresh local session unable to open the composer.
    source_before_quota = source
    # Expose the existing native router for the world page and settings entry.
    router_anchor = 'const Db=async()=>'
    if 'window.__oliviaNativeView=' not in source and router_anchor in source:
        source = source.replace(router_anchor,
            'window.__oliviaNativeView={router:Ea,h:mo};'
            'window.dispatchEvent(new Event("olivia-native-router-ready"));' + router_anchor, 1)
    # 芙桃's local catalog integration: expose only the existing reactive catalog.
    source = source.replace(
        'return{songs:e,musicStyles:t,performanceModes:s,loaded:i,',
        'return window.__oliviaLocalSongCatalog={songs:e,musicStyles:t,performanceModes:s,loaded:i,',
    ).replace(
        'e.value=h.songs,t.value=h.musicStyles,s.value=h.performanceModes}',
        'e.value=h.songs,t.value=h.musicStyles,s.value=h.performanceModes;'
        'window.dispatchEvent(new Event("olivia-local-catalog-ready"))}',
    )
    # Remove the early acceptance patch that treated missing native cache as ready.
    local_status = 'if(window.__oliviaLocalSongCatalog?.songs.value.some(q=>q.oliviaLocal&&q.id===U.songId))U.exist=true;'
    source = source.replace(local_status, '')
    source = source.replace(
        '_e(()=>w.value&&oe.loaded,q=>{q&&Ra()},{immediate:!0})',
        '_e(()=>w.value&&oe.loaded&&oe.songs,q=>{q&&Ra()},{immediate:!0})',
    )
    source = _repair_mailbox_waiting_footer(source)
    # Keep the native sealed-letter animation; only specialize its caption.
    source = source.replace(
        'letterStatus:e.letterStatus,auditStatus:e.auditStatus,',
        'letterStatus:e.letterStatus,videoPending:e.videoPending===true,auditStatus:e.auditStatus,',
    ).replace(
        '__name:"MailBoxReplyContent",props:{modelValue:{},',
        '__name:"MailBoxReplyContent",props:{videoPending:{type:Boolean},modelValue:{},',
    ).replace(
        'v(o(i)("mailbox_waiting_for_reply"))',
        'v(A.videoPending?"林离录视频中":o(i)("mailbox_waiting_for_reply"))',
    ).replace(
        'F(ks,{key:1,"model-value":',
        'F(ks,{key:1,videoPending:i.mail.videoPending,"model-value":',
    ).replace(
        'F(ks,{key:0,ref_key:"replyContentRef",',
        'F(ks,{key:0,videoPending:i.mail.videoPending,ref_key:"replyContentRef",',
    ).replace(
        '["model-value","videoUrl","timestamp","type"]',
        '["model-value","videoUrl","timestamp","type","videoPending"]',
    ).replace(
        '["modelValue","videoUrl","timestamp","type"]',
        '["modelValue","videoUrl","timestamp","type","videoPending"]',
    )
    source = source.replace(
        're.isUnread!==Ee.isUnread)',
        're.isUnread!==Ee.isUnread||re.videoPending!==Ee.videoPending||re.letterStatus!==Ee.letterStatus)',
    ).replace(
        're.isUnread!==Ee.isUnread||re.letterStatus!==Ee.letterStatus)',
        're.isUnread!==Ee.isUnread||re.videoPending!==Ee.videoPending||re.letterStatus!==Ee.letterStatus)',
    )
    # A list row has no reply body/video URL. Reload an open detail on delivery
    # instead of marking the empty summary as an already-loaded reply.
    source = source.replace(
        't.value[ye]=re)}}N()',
        't.value[ye]=re,Ee.detailLoaded&&await z(re.id))}}N()',
    )
    offline_guard = 'Te.interceptors.request.use(e=>{const t=Ie();if(t.isOfflineMode)throw new Ol(e);'
    if offline_guard in source:
        source = source.replace(
            offline_guard,
            _LOCAL_MAILBOX_REQUEST_HELPER + offline_guard.replace(
                'if(t.isOfflineMode)', 'if(t.isOfflineMode&&!oliviaLocalMailboxRequest(e))'
            ),
            1,
        )
    source = source.replace(
        'He(()=>{p.value||d.fetchMailList(!0)})',
        'He(()=>{d.fetchMailList(!0),d.startPolling()})',
    )
    route_anchor = 'Te.interceptors.request.use(e=>{const t=Ie();if(t.isOfflineMode&&!oliviaLocalMailboxRequest(e))'
    source = source.replace(route_anchor,
        'Te.interceptors.request.use(async e=>{if(oliviaLocalMailboxRequest(e)&&window.__oliviaPrepareLetterRoute)e=await window.__oliviaPrepareLetterRoute(e);const t=Ie();if(t.isOfflineMode&&!oliviaLocalMailboxRequest(e))')
    source = source.replace(
        'timestamp:e.createdAt*1e3',
        'timestamp:e.createdAt==null?null:e.createdAt*1e3',
    ).replace(
        'timestamp:(e.repliedAt??e.createdAt)*1e3',
        'timestamp:(e.repliedAt??e.createdAt)==null?null:(e.repliedAt??e.createdAt)*1e3',
    ).replace(
        'Ws=e=>{const t=new Date(e),',
        'Ws=e=>{if(e==null)return"时间未知";const t=new Date(e),',
    )
    source = source.replace('a.timestamp?Hs(a.timestamp):""', 'a.timestamp?Ws(a.timestamp):""')
    source = source.replace('I=j(()=>Hs(l.timestamp))', 'I=j(()=>Ws(l.timestamp))')
    source = source.replace('v(o(a)("common_beta_tag"))', 'v("Resonance Edition")')
    source = source.replace('letterStatus:e.letterStatus,', 'replyKind:e.replyKind||"text_letter",letterStatus:e.letterStatus,') if 'replyKind:e.replyKind' not in source else source
    native_icon = 'n("div",{class:ae(["mail-item-icon",o(a).iconBgClass])},[k(p,{type:o(a).iconType,class:ae(["text-[24px]",o(a).iconClass])},null,8,["type","class"])],2)'
    if 'n("olivia-mail-kind"' not in source:
        source = source.replace(native_icon, '(m.mail.received&&m.mail.received.type!=="video"&&["voice_reply","singing_video","voice_song_video"].includes(m.mail.replyKind)?n("olivia-mail-kind",{kind:m.mail.replyKind},null,8,["kind"]):' + native_icon + ')')
    source = source.replace(
        'const uo=st("mailbox",()=>{const{t:e}=fe(),t=b([]),s=b(0),',
        'const uo=st("mailbox",()=>{const{t:e}=fe(),t=b([]),s=b(99),',
    ).replace(
        'function O(){R(),t.value=[],s.value=0,i.value=0,m.value=0,',
        'function O(){R(),t.value=[],s.value=99,i.value=0,m.value=0,',
    )
    # Migrate the old always-visible offline patch and refresh terminal status
    # even when unread/media fields are unchanged (notably pending -> failed).
    source = source.replace('"hide-write":!1', MAILBOX_WRITE_ANCHOR_0627)
    source = source.replace(
        're.isUnread!==Ee.isUnread)&&',
        're.isUnread!==Ee.isUnread||re.letterStatus!==Ee.letterStatus)&&',
    )
    source = _repair_native_letter_audio(source)
    source = _repair_native_proactive_collection(source)
    anchor_count = source.count(MAILBOX_WRITE_ANCHOR_0627)
    replacement_count = source.count(MAILBOX_WRITE_REPLACEMENT_0627)
    if anchor_count == 1 and replacement_count == 0:
        _write_utf8(
            main,
            source.replace(
                MAILBOX_WRITE_ANCHOR_0627,
                MAILBOX_WRITE_REPLACEMENT_0627,
                1,
            ),
        )
        return "PATCHED"
    if anchor_count == 0 and replacement_count == 1:
        if source != source_before_quota:
            _write_utf8(main, source)
            return "PATCHED"
        return "ALREADY_PATCHED"
    raise CompanionSettingsPatchError(
        "COMPANION_MAILBOX_WRITE_ANCHOR_INVALID"
    )


def _verify_archive(
    path: Path,
    *,
    api_base: str,
    original_hashes: dict[str, str],
    mailbox_write_changed: bool = False,
) -> None:
    patched_hashes = _member_hashes(path)
    if set(patched_hashes) != set(original_hashes) | {BOOTSTRAP_MEMBER}:
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")
    for name, digest in original_hashes.items():
        if name in {INDEX_MEMBER, BOOTSTRAP_MEMBER} or (
            mailbox_write_changed and name == MAIN_JS_0627
        ):
            continue
        if patched_hashes.get(name) != digest:
            raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")
    try:
        with zipfile.ZipFile(path) as archive:
            index = archive.read(INDEX_MEMBER).decode("utf-8")
            bootstrap = archive.read(BOOTSTRAP_MEMBER).decode("utf-8")
            main_0627 = (
                archive.read(MAIN_JS_0627).decode("utf-8")
                if mailbox_write_changed
                else ""
            )
    except (KeyError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED") from exc
    required = (
        'data-olivia-companion-settings="p03.original-settings-shell.v1"',
        f'data-ui-version="{SETTINGS_UI_VERSION}"',
        f'data-api-base="{html.escape(api_base, quote=True)}"',
    )
    bootstrap_required = (
        'const STATUS_PATH = "/toy/companion/status";',
        'const PROACTIVE_STATUS_PATH = "/toy/proactive/status";',
        'const PROACTIVE_SETTINGS_PATH = "/toy/proactive/settings";',
        'const MEMORY_PATH = "/toy/companion/memory";',
        'const LOCAL_LETTER_IMPORT_PATH = "/toy/letter/legacy/local-import";',
        'const MEMORY_CORRECT_PATH = "/toy/companion/memory/correct";',
        'const MEMORY_DELETE_PATH = "/toy/companion/memory/delete";',
        'const MEMORY_PAUSE_PATH = "/toy/companion/memory/pause";',
        'const MEMORY_RESUME_PATH = "/toy/companion/memory/resume";',
        'const CONFIRM_HEADER = "X-Olivia-Companion-Action";',
        'const CONFIRM_VALUE = "confirmed";',
        'method: "GET"',
        'method: "POST"',
        "confirmAction",
        "login_check_enabled",
        "data-olivia-proactive-settings",
        "林离正在写信",
        "data-olivia-companion-settings-root",
        "panel.dataset.oliviaCompanionPanel",
        "长期记忆",
        "林离世界",
        "纠正",
        "删除",
        "暂停长期记忆",
        "恢复长期记忆",
        "导入本地历史信件",
        "new MutationObserver",
        "replaceChildren",
    )
    forbidden = (
        "<iframe",
        "window.open",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        'method: "PUT"',
        'method: "PATCH"',
        'method: "DELETE"',
        "eval(",
        "new Function",
        "CANDIDATES_PATH",
        "待确认的关系建议",
        "批准",
        "拒绝",
        "本地世界线",
        "approve",
        "reject",
    )
    if (
        any(value not in index for value in required)
        or any(value not in bootstrap for value in bootstrap_required)
        or any(value in bootstrap for value in forbidden)
        or _normalize_newlines(bootstrap)
        != _normalize_newlines(BOOTSTRAP_JAVASCRIPT)
        or (
            mailbox_write_changed
            and (
                main_0627.count(MAILBOX_WRITE_REPLACEMENT_0627) != 1
                or MAILBOX_WRITE_ANCHOR_0627 in main_0627
                or (
                    'const Db=async()=>' in main_0627
                    and 'window.__oliviaNativeView={router:Ea,h:mo};' not in main_0627
                )
            )
        )
    ):
        raise CompanionSettingsPatchError("COMPANION_PATCH_VERIFICATION_FAILED")


def patch_companion_settings(
    feapp_path: str | os.PathLike[str],
    api_base: str | None,
    *,
    work_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Patch repository-owned UI and restore the supported mailbox entry."""

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

    with tempfile.TemporaryDirectory(
        prefix=".patch-companion-settings-",
        dir=sandbox,
    ) as name:
        temporary_root = Path(name)
        rollback = temporary_root / "rollback.dat"
        _atomic_copy(feapp, rollback)
        try:
            unpacked = temporary_root / "unpacked"
            with zipfile.ZipFile(feapp) as archive:
                _safe_extract(archive, unpacked)
            ui_status = _patch_index(unpacked, normalized_api_base)
            mailbox_status = _repair_mailbox_write_access(unpacked)
            status = (
                "PATCHED"
                if "PATCHED" in {ui_status, mailbox_status}
                else "ALREADY_PATCHED"
            )
            mailbox_write_changed = mailbox_status == "PATCHED"
            if status == "PATCHED":
                output = temporary_root / "patched.dat"
                _repack(unpacked, output)
                os.replace(output, feapp)
            _verify_archive(
                feapp,
                api_base=normalized_api_base,
                original_hashes=original_hashes,
                mailbox_write_changed=mailbox_write_changed,
            )
        except Exception:
            _atomic_copy(rollback, feapp)
            raise

    return {
        "schema_version": PATCH_SCHEMA_VERSION,
        "ui_version": SETTINGS_UI_VERSION,
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


def _repair_native_letter_audio(source: str) -> str:
    """Extend native props and paper content; keep original imagery and type."""
    # Native downloads return task IDs before completion. Queue both files once
    # and track every task; two calls would overwrite the active task/poller.
    source = source.replace('sourceUrls:[B],destPath:K', 'sourceUrls:Array.isArray(B)?B:[B],destPath:K')
    source = source.replace('ue=Object.values(W.data)[0];he.value=ue', 'ue=Object.values(W.data);he.value=ue')
    source = source.replace('e1([he.value]),ye=re[0];if(!ye)return;',
        'e1(Array.isArray(he.value)?he.value:[he.value]),ye={totalBytes:re.reduce((s,t)=>s+t.totalBytes,0),'
        'downloadedBytes:re.reduce((s,t)=>s+t.downloadedBytes,0),state:re.length&&re.every(t=>t.state===zo.Completed)?zo.Completed:'
        're.some(t=>t.state===zo.Failed||t.state===zo.Cancelled)?zo.Failed:null};if(!re.length)return;')
    source = source.replace('t1([he.value])', 't1(Array.isArray(he.value)?he.value:[he.value])')
    source = source.replace(
        'const replyAudio=M.value?.received?.audioUrl;if(replyAudio){yt.hide();await d.startVideoDownload(replyAudio,R);return}',
        'const replyAudio=M.value?.received?.audioUrl;const replySong=M.value?.received?.songUrl;'
        'if(replyAudio||replySong){yt.hide();await d.startVideoDownload([replyAudio,replySong].filter(Boolean),R);return}')
    source = source.replace(
        'O.replyTextImage&&await yn(O.replyTextImage,`${R}/mail-${H}-reply.png`),yt.hide(),Ds(R)',
        'O.replyTextImage&&await yn(O.replyTextImage,`${R}/mail-${H}-reply.png`);'
        'const replyAudio=M.value?.received?.audioUrl;'
        'const replySong=M.value?.received?.songUrl;'
        'if(replyAudio||replySong){yt.hide();await d.startVideoDownload([replyAudio,replySong].filter(Boolean),R);return}'
        'yt.hide(),Ds(R)',
    )
    source = source.replace(
        'A.videoPending?"林离录视频中":o(i)("mailbox_waiting_for_reply")',
        'A.videoPending?"林离录视频中":["PENDING","QUEUED","PROCESSING"].includes(A.audioStatus)?"林离正在录语音…":o(i)("mailbox_waiting_for_reply")',
    )
    def bath_progress(value):
        # Bundles can contain already-patched props but an unpatched caption.
        # Guard each anchor independently so repeat installation stays stable.
        for before, after in (
            ('audioStatus:e.audioStatus||"",', 'replyWaitReason:e.replyWaitReason||"",audioStatus:e.audioStatus||"",'),
            ('__name:"MailBoxReplyContent",props:{', '__name:"MailBoxReplyContent",props:{replyWaitReason:{},'),
            ('F(ks,{coverId:i.mail.coverId,', 'F(ks,{replyWaitReason:i.mail.replyWaitReason,coverId:i.mail.coverId,'),
            ('"audioUrl","audioStatus","songUrl",', '"replyWaitReason","audioUrl","audioStatus","songUrl",'),
            ('A.videoPending?"林离录视频中":', 'A.replyWaitReason==="bathing"?"林离洗澡中":A.videoPending?"林离录视频中":'),
            ('re.isUnread!==Ee.isUnread', 're.replyWaitReason!==Ee.replyWaitReason||re.isUnread!==Ee.isUnread'),
        ):
            if after not in value:
                value = value.replace(before, after)
        return value
    def cover_progress(value):
        if 'coverId:e.coverId' in value:
            return bath_progress(value)
        value = value.replace('audioStatus:e.audioStatus||"",', 'coverId:e.coverId||"",audioStatus:e.audioStatus||"",')
        value = value.replace('props:{audioUrl:{},', 'props:{coverId:{},audioUrl:{},')
        value = value.replace('F(ks,{audioUrl:', 'F(ks,{coverId:i.mail.coverId,audioUrl:')
        value = value.replace('["audioUrl","audioStatus","songUrl",', '["coverId","audioUrl","audioStatus","songUrl",')
        value = value.replace('{"audio-url":A.audioUrl', '{"cover-id":A.coverId||"","audio-url":A.audioUrl')
        return bath_progress(value.replace('["audio-url","audio-status","song-url"]', '["cover-id","audio-url","audio-status","song-url"]'))
    if 'olivia-letter-audio' in source:
        return cover_progress(source)
    source = source.replace('letterStatus:e.letterStatus,',
        'audioStatus:e.audioStatus||"",audioRevision:e.audioRevision||"",letterStatus:e.letterStatus,')
    source = source.replace('videoUrl:e.replyVideoUrl||void 0',
        'audioUrl:e.replyAudioUrl||"",songUrl:e.replySongUrl||"",videoUrl:e.replyVideoUrl||void 0')
    source = source.replace('__name:"MailBoxReplyContent",props:{',
        '__name:"MailBoxReplyContent",props:{audioUrl:{},audioStatus:{},songUrl:{},')
    source = source.replace('F(ks,{key:',
        'F(ks,{audioUrl:i.mail.received?.audioUrl,audioStatus:i.mail.audioStatus,songUrl:i.mail.received?.songUrl,key:')
    source = source.replace('["modelValue","videoUrl","timestamp","type"',
        '["audioUrl","audioStatus","songUrl","modelValue","videoUrl","timestamp","type"')
    source = source.replace('["model-value","videoUrl","timestamp","type"',
        '["audioUrl","audioStatus","songUrl","model-value","videoUrl","timestamp","type"')
    source = source.replace('re.isUnread!==Ee.isUnread',
        're.audioRevision!==Ee.audioRevision||re.audioStatus!==Ee.audioStatus||re.isUnread!==Ee.isUnread')
    anchor='[A.type==="error"?'
    replacement='[A.type==="text"&&(A.audioUrl||A.audioStatus)?n("olivia-letter-audio",{"audio-url":A.audioUrl||"","audio-status":A.audioStatus||"","song-url":A.songUrl||""},null,8,["audio-url","audio-status","song-url"]):Y("",!0),A.type==="error"?'
    source = source.replace(anchor,replacement,1)
    return cover_progress(source)


_NATIVE_PROACTIVE_MARKER = "/*olivia-proactive-native-v1*/"


def _repair_native_proactive_collection(source: str) -> str:
    """Project proactive rows into the original Collection welcome-paper path."""

    if _NATIVE_PROACTIVE_MARKER in source:
        return source

    # The supported 0.0.9.627 bundle first maps the list/detail wire payload
    # into `bn`/`p1`, then renders the built-in welcome paper for `__welcome__`.
    # Keep that shape and only add the fields needed to make a proactive row
    # title-only on the left and reply-only on the right.
    model_prefix = (
        'return{id:e.letterId,isUnread:e.isRead===0,coverId:e.coverId||"",'
    )
    model_replacement = (
        'return{id:e.letterId,isUnread:e.isRead===0,'
        'origin:e.origin||"",title:e.title||e.summary||"",'
        'replyAllowed:e.replyAllowed!==false,coverId:e.coverId||"",'
    )
    if source.count(model_prefix) < 2:
        return source
    required = (
        ':x.selectedMail&&!o(p)?',
        ':o(p)&&o(l).welcomeMailRead?',
        'h=j({get:()=>i("mailbox_welcome_content"),set:()=>{}})',
        '"is-visible":!0,readonly:"",timestamp:((M=(E=x.selectedMail)==null?void 0:E.received)==null?void 0:M.timestamp)??0,type:"text"}',
    )
    if any(source.count(anchor) != 1 for anchor in required):
        raise CompanionSettingsPatchError('COMPANION_PROACTIVE_ANCHOR_INVALID')
    source = source.replace(model_prefix, model_replacement)
    source = source.replace(
        'sent:{subject:e.summary,',
        'sent:{subject:e.title||e.summary,',
        1,
    )
    source = source.replace(
        'received:t?{subject:e.summary,',
        'received:t?{subject:e.title||e.summary,',
        1,
    )
    source = source.replace(
        'const t=e.content.length>20?e.content.slice(0,20)+"...":e.content;',
        'const t=e.origin==="proactive"?(e.title||e.summary||""):'
        'e.content.length>20?e.content.slice(0,20)+"...":e.content;',
        1,
    )
    source = source.replace(
        'sent:{subject:t,timestamp:e.createdAt==null?null:e.createdAt*1e3,content:e.content}',
        'sent:{subject:t,timestamp:e.createdAt==null?null:e.createdAt*1e3,'
        'content:e.origin==="proactive"?"":e.content}',
        1,
    )
    source = source.replace(
        'h=j({get:()=>i("mailbox_welcome_content"),set:()=>{}})',
        'h=j({get:()=>{var x;return((x=a.selectedMail)==null?void 0:x.origin)==="proactive"'
        '?((x=a.selectedMail.received)==null?void 0:x.content)||"":i("mailbox_welcome_content")},set:()=>{}})',
        1,
    )
    source = source.replace(
        ':x.selectedMail&&!o(p)?',
        ':x.selectedMail&&!o(p)&&x.selectedMail.origin!=="proactive"?',
        1,
    )
    source = source.replace(
        ':o(p)&&o(l).welcomeMailRead?',
        ':(o(p)&&o(l).welcomeMailRead||x.selectedMail&&x.selectedMail.origin==="proactive")?',
        1,
    )
    source = source.replace(
        'k(ks,{modelValue:o(h),"onUpdate:modelValue":I[1]||(I[1]=A=>be(h)?h.value=A:null),'
        'class:"w-[516px] aspect-[16/9]","is-visible":!0,readonly:"",'
        'timestamp:((M=(E=x.selectedMail)==null?void 0:E.received)==null?void 0:M.timestamp)??0,type:"text"},null,8,["modelValue","timestamp"]',
        'k(ks,{coverId:x.selectedMail?.coverId,audioUrl:x.selectedMail?.received?.audioUrl||"",'
        'audioStatus:x.selectedMail?.audioStatus||"",songUrl:x.selectedMail?.received?.songUrl||"",'
        'modelValue:o(h),"onUpdate:modelValue":I[1]||(I[1]=A=>be(h)?h.value=A:null),'
        'class:"w-[516px] aspect-[16/9]","is-visible":!0,readonly:"",'
        'timestamp:((M=(E=x.selectedMail)==null?void 0:E.received)==null?void 0:M.timestamp)??0,type:"text"},null,8,["modelValue","timestamp","coverId","audioUrl","audioStatus","songUrl"]',
        1,
    )
    return _NATIVE_PROACTIVE_MARKER + source
