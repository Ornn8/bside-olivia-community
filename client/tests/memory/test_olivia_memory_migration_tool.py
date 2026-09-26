"""Synthetic packaging checks for the community Olivia memory migration tool.

The tool ships as three standalone files under ``tools/olivia_memory/``:
``olivia_memory.py``, ``运行.bat`` and ``说明.docx`` (the illustrated user
manual; a plain-text variant was retired upstream).  These tests keep the
shipped bundle safe to redistribute: the launcher stays pure ASCII with CRLF
endings, the script carries no invisible characters, and none of the files
leak machine-specific absolute paths, user names or real letter fragments.
"""

import re
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
