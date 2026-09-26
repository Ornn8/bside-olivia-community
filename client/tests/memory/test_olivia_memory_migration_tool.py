"""Synthetic packaging checks for the community Olivia memory migration tool.

The tool ships as three standalone files under ``tools/olivia_memory/``:
``olivia_memory.py``, ``运行.bat`` and ``说明.docx`` (the illustrated user
manual; a plain-text variant was retired upstream).  These tests keep the
shipped bundle safe to redistribute: the launcher stays pure ASCII with CRLF
endings, the script carries no invisible characters, and none of the files
leak machine-specific absolute paths, user names or real letter fragments.
"""

import contextlib
import importlib.util
import io
import json
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[2] / "tools" / "olivia_memory"
PY_FILE = TOOL_DIR / "olivia_memory.py"
BAT_FILE = TOOL_DIR / "运行.bat"
DOCX_FILE = TOOL_DIR / "说明.docx"

INVISIBLE = (
    "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"
)

BANNED_PATH_FRAGMENTS = (
    "C:\\Users\\",
    str(Path.home()),
    "\\AppData\\Local\\",
    "C:\\Users",
)


def _docx_text() -> str:
    """Extract the concatenated visible text from the shipped docx."""
    with zipfile.ZipFile(DOCX_FILE, "r") as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    text = re.sub(r"<[^>]+>", "", xml)
    for a, b in (
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&amp;", "&"),
        ("&quot;", '"'),
    ):
        text = text.replace(a, b)
    return text


def test_bundle_files_exist():
    for path in (PY_FILE, BAT_FILE, DOCX_FILE):
        assert path.is_file(), path
        assert path.stat().st_size > 0


def test_script_compiles(tmp_path):
    import py_compile

    py_compile.compile(
        str(PY_FILE), cfile=str(tmp_path / "olivia_memory.pyc"), doraise=True
    )


def test_launcher_is_pure_ascii():
    raw = BAT_FILE.read_bytes()
    non_ascii = [byte for byte in raw if byte > 127]
    assert not non_ascii, "launcher must stay pure ASCII for every code page"


def test_launcher_uses_crlf_and_no_chcp():
    text = BAT_FILE.read_bytes().decode("ascii")
    lone_lf = [i for i, ch in enumerate(text) if ch == "\n" and (i == 0 or text[i - 1] != "\r")]
    assert not lone_lf, "launcher must keep CRLF line endings"
    for line in text.splitlines():
        if line.strip().lower().startswith("chcp"):
            pytest.fail("launcher must not call chcp")


def test_no_invisible_characters():
    body = PY_FILE.read_text(encoding="utf-8")
    for lineno, line in enumerate(body.split("\n"), start=1):
        for ch in INVISIBLE:
            if ch in line:
                pytest.fail(f"olivia_memory.py:{lineno} contains invisible character U+{ord(ch):04X}")


@pytest.mark.parametrize("path", [PY_FILE, BAT_FILE])
def test_no_machine_absolute_paths(path):
    body = path.read_text(encoding="utf-8-sig", errors="replace")
    for lineno, line in enumerate(body.split("\n"), start=1):
        for needle in BANNED_PATH_FRAGMENTS:
            if needle.lower() in line.lower():
                pytest.fail(f"{path.name}:{lineno} leaks machine path fragment {needle!r}")


def test_docx_text_has_no_machine_paths():
    text = _docx_text()
    for needle in BANNED_PATH_FRAGMENTS:
        assert needle.lower() not in text.lower(), (
            f"说明.docx leaks machine path fragment {needle!r}"
        )


def test_script_has_no_bom():
    raw = PY_FILE.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "script must not start with a UTF-8 BOM"


def test_docx_metadata_has_no_real_name():
    """The shipped docx must not carry the author's real name in core properties."""
    with zipfile.ZipFile(DOCX_FILE, "r") as zf:
        core = zf.read("docProps/core.xml").decode("utf-8")
    assert "金灿灿" not in core, "docx core.xml still contains real-name metadata"
    assert "Olivia Community" in core, "docx lastModifiedBy should be neutral"


# ---------------------------------------------------------------------------
# Migration behaviour.  The checks above only look at the shipped bundle; these
# drive the tool against synthetic installs, so a change of behaviour cannot
# ride along unnoticed.  Nothing here touches a real mailbox or client process.
# ---------------------------------------------------------------------------

BODY = "杯底画了一只蜗牛，围巾是橙色的。"
LIBRARY_REPLY = "记下了，橙色围巾。"
SOURCE_REPLY = "我记得那只蜗牛，围巾也是橙色的。"


def _tool_module():
    """Load the shipped script as a module.  It guards ``__main__``, so this
    only defines functions: nothing runs and no user data is touched."""
    name = "olivia_memory_under_test"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, PY_FILE)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _empty_install(tmp_path):
    """The smallest directory ``find_install`` accepts: the markers it looks
    for plus a state.json the tool can read."""
    root = tmp_path / "olivia"
    data = root / "install" / "data"
    (data / "memory").mkdir(parents=True)
    (data / "state.json").write_text(
        json.dumps({"letters": []}, ensure_ascii=False), encoding="utf-8"
    )
    return root


def _source_file(tmp_path, letters) -> Path:
    """A source file in the format the tool reads back (olivia.letters.v1)."""
    from runtime.imports.letter_backup import export_letters

    path = tmp_path / "letters.json"
    path.write_text(
        json.dumps(export_letters(letters), ensure_ascii=False), encoding="utf-8"
    )
    return path


def _checked_out_elsewhere(tmp_path):
    """One letter that is in the library and in the source file, with a
    different reply on each side: the case menu 2 asks the user to decide."""
    from runtime.imports.letter_backup import export_letters, import_letters
    from runtime.memory.local_memory import LocalMemoryAdapter

    install = _empty_install(tmp_path)
    db = install / "install" / "data" / "memory" / "memory.sqlite3"
    with LocalMemoryAdapter(db) as adapter:
        import_letters(
            export_letters([{
                "letter_id": "lib-1", "content": BODY, "reply_text": LIBRARY_REPLY,
                "created_at": 1788000000,
            }]),
            adapter=adapter,
        )
    src = _source_file(tmp_path, [{
        "letter_id": "src-1", "content": BODY, "reply_text": SOURCE_REPLY,
        "created_at": 1788000000,
    }])
    return install, src


def test_state_order_never_rewrites_a_hidden_letter(tmp_path, monkeypatch):
    """Hidden letters still occupy a slot in state.json.  Planning on the
    filtered array and then writing with those indexes hit the letter in front
    of the target, and rebuilding the old value made the check pass anyway."""
    from runtime.memory.local_memory import LocalMemoryAdapter

    tool = _tool_module()
    install = _empty_install(tmp_path)
    with LocalMemoryAdapter(install / "install" / "data" / "memory" / "memory.sqlite3"):
        pass          # an empty but real library table is all this case needs
    state = install / "install" / "data" / "state.json"
    state.write_text(
        json.dumps({"letters": [
            {"letter_id": "hidden-1", "content": "被藏起来的那封",
             "reply_text": "这封不该被碰", "created_at": 1800000000,
             "superseded_by": "tool-hidden"},
            {"letter_id": "letter-a", "content": "第一封", "reply_text": "回信一",
             "created_at": 1800000000},
            {"letter_id": "letter-b", "content": "第二封", "reply_text": "回信二",
             "created_at": 1800000000},
        ]}, ensure_ascii=False), encoding="utf-8"
    )
    src = _source_file(tmp_path, [
        {"letter_id": "src-a", "content": "第一封", "reply_text": "回信一",
         "created_at": 1800000000},
        {"letter_id": "src-b", "content": "第二封", "reply_text": "回信二",
         "created_at": 1800000000},
    ])
    monkeypatch.setattr(tool, "stop_yueli", lambda _install: None)
    monkeypatch.setattr(tool, "start_yueli", lambda _install: None)

    assert tool.cmd_apply(src, str(install)) == 0

    after = json.loads(state.read_text(encoding="utf-8"))["letters"]
    assert after[0]["created_at"] == 1800000000, "the hidden letter was rewritten"
    assert after[0]["superseded_by"] == "tool-hidden"
    assert [after[1]["created_at"], after[2]["created_at"]] == [1800000001, 1800000000]


def test_library_choice_keeps_backup_record_and_survives_export(tmp_path, monkeypatch):
    """The product exports from ``backup_record`` (letter_backup.export_letters),
    so picking the library version has to be written there too, or the next
    export - import round trip silently replaces the text the user kept."""
    tool = _tool_module()
    from runtime.imports.letter_backup import export_letters
    from runtime.memory.local_memory import LocalMemoryAdapter

    install, src = _checked_out_elsewhere(tmp_path)
    db = install / "install" / "data" / "memory" / "memory.sqlite3"
    monkeypatch.setattr(tool, "stop_yueli", lambda _install: None)
    monkeypatch.setattr(tool, "start_yueli", lambda _install: None)

    assert tool.cmd_apply(src, str(install), near="library") == 0

    with sqlite3.connect(db) as con:
        meta = json.loads(
            con.execute("SELECT metadata_json FROM legacy_letters").fetchone()[0])
    assert meta["user_content"] == BODY
    assert meta["reply_text"] == LIBRARY_REPLY
    assert meta["backup_record"]["content"] == BODY
    assert meta["backup_record"]["reply_text"] == LIBRARY_REPLY
    assert meta["backup_record"]["reply_text"] == meta["reply_text"]

    with LocalMemoryAdapter(db) as adapter:
        exported = export_letters(adapter.list_legacy())
    assert [row["reply_text"] for row in exported["letters"]] == [LIBRARY_REPLY]


def test_both_choices_keep_their_own_backup_record(tmp_path, monkeypatch):
    """「两个都保留」keeps the library version where it is and adds the source
    version next to it.  Each row has to carry its own text in backup_record,
    or an export - import round trip collapses them back into one version."""
    tool = _tool_module()
    from runtime.imports.letter_backup import export_letters
    from runtime.memory.local_memory import LocalMemoryAdapter

    install, src = _checked_out_elsewhere(tmp_path)
    db = install / "install" / "data" / "memory" / "memory.sqlite3"
    monkeypatch.setattr(tool, "stop_yueli", lambda _install: None)
    monkeypatch.setattr(tool, "start_yueli", lambda _install: None)

    assert tool.cmd_apply(src, str(install), near="both") == 0

    with sqlite3.connect(db) as con:
        metas = [json.loads(row[0]) for row in
                 con.execute("SELECT metadata_json FROM legacy_letters")]
    assert len(metas) == 2, "the library version stays and the source version is added"
    for meta in metas:
        assert meta["backup_record"]["content"] == meta["user_content"]
        assert meta["backup_record"]["reply_text"] == meta["reply_text"]
    assert sorted(m["reply_text"] for m in metas) == sorted([LIBRARY_REPLY, SOURCE_REPLY])

    with LocalMemoryAdapter(db) as adapter:
        exported = export_letters(adapter.list_legacy())
    assert sorted(row["reply_text"] for row in exported["letters"]) == \
        sorted([LIBRARY_REPLY, SOURCE_REPLY])


def test_runtime_log_never_receives_letter_bodies(tmp_path, monkeypatch):
    """Letter text belongs on screen and in the report the user asked for; the
    persisted diagnostic log is pasted into issue reports, so it only records
    counts, identifiers and error codes."""
    tool = _tool_module()
    install, src = _checked_out_elsewhere(tmp_path)
    log_dir = tmp_path / "tool"
    log_dir.mkdir()
    monkeypatch.setattr(tool, "__file__", str(log_dir / "olivia_memory.py"))

    tool.open_log()
    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console):
            tool.cmd_compare(src, str(install))
            tool.log_order_preview([(1788000000, [(1788000000, {
                "letter_id": "preview-1", "content": BODY,
                "reply_text": LIBRARY_REPLY})])])
    finally:
        if tool._LOG is not None:
            tool._LOG.close()
        tool._LOG = None

    shown = console.getvalue()
    logged = (log_dir / "olivia_memory.log").read_text(encoding="utf-8")
    for text in (BODY, LIBRARY_REPLY, SOURCE_REPLY):
        assert text in shown, "the user still has to be able to read this on screen"
        assert text not in logged, "letter text leaked into the diagnostic log"
    assert "同一分钟" in logged, "the log still records times and counts"
    assert "preview-1" in logged, "the log still records which letter it was"
