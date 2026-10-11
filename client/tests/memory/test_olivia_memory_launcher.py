"""Run launcher branches with synthetic interpreters, never the installed tool."""
import os
from pathlib import Path
import shutil
import subprocess
import venv

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows cmd.exe launcher")
LAUNCHER = Path(__file__).resolve().parents[2] / "tools/olivia_memory/运行.bat"


@pytest.fixture(scope="module")
def installs(tmp_path_factory):
    root = tmp_path_factory.mktemp("launcher")
    folders = {"normal": root / "Olivia with spaces (copy)",
               "bang": root / "Olivia! with spaces (copy)",
               "nested": root / "Olivia parent" / "bundle" / "release"}
    for folder in folders.values():
        python = folder / "runtime/python-test"
        venv.EnvBuilder(with_pip=False).create(python)
        # The redirector also works beside pyvenv.cfg, matching the embedded
        # runtime/python-*/python.exe layout without bundling a binary fixture.
        shutil.copy2(python / "Scripts/python.exe", python / "python.exe")
        subprocess.run([str(python / "python.exe"), "-c", "import sqlite3,json"],
                       check=True, capture_output=True, timeout=15)
    return folders


def run_branch(tmp_path, body, folder, *, stdin=""):
    script = tmp_path / "run.cmd"
    script.write_bytes(body.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))
    (tmp_path / "olivia_memory.py").write_text('print("SYNTHETIC_TOOL_STARTED")\n', encoding="ascii")
    environment = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    windows = os.environ["SystemRoot"]
    environment.update(PATH=str(Path(windows) / "System32") + ";" + windows,
                       TEST_ROOT=str(folder))
    return subprocess.run([os.environ["COMSPEC"], "/d", "/c", "run.cmd"],
                          cwd=tmp_path, env=environment, input=stdin.encode("ascii"),
                          capture_output=True, timeout=15)


def launcher():
    return LAUNCHER.read_text(encoding="ascii")


@pytest.mark.parametrize("kind", ["normal", "bang"])
@pytest.mark.parametrize("level", ["root", "runtime", "interpreter"])
def test_dragged_folder_starts_tool_and_remembers_exact_path(tmp_path, installs, kind, level):
    install = installs[kind]
    folder = {"root": install, "runtime": install / "runtime",
              "interpreter": install / "runtime/python-test"}[level]
    text = launcher()
    # Bypass automatic discovery only: a Python installed on the test host
    # must not prevent us from reaching the real manual-input branch.
    prefix = text[:text.index("rem ---- 1)")]
    manual = prefix + text[text.index("rem ---- 5)"):]
    (tmp_path / "python_path.txt").write_text('"' + str(tmp_path / "missing/python.exe") + '"\n', encoding="ascii")
    completed = run_branch(tmp_path, manual, folder, stdin='"' + str(folder) + '"\r\n\r\n')
    assert completed.returncode == 0, "manual discovery failed"
    assert b"SYNTHETIC_TOOL_STARTED" in completed.stdout, "tool was not started"
    saved = (tmp_path / "python_path.txt").read_text(encoding="ascii").rstrip("\r\n")
    expected = '"' + str(install / "runtime/python-test/python.exe") + '"'
    assert (saved == expected), "remembered path changed or contains trailing whitespace"

    # Exercise the actual cached-path branch independently of host discovery.
    cached = text[:text.index("rem ---- 1)")]
    cached += "if defined PY goto :run\nexit /b 1\n" + text[text.index(":run\n"):]
    completed = run_branch(tmp_path, cached, folder, stdin="\r\n")
    assert completed.returncode == 0, "cached interpreter failed"
    assert b"SYNTHETIC_TOOL_STARTED" in completed.stdout, "cached path was not reused"


def test_candidate_root_still_finds_deeper_runtime(tmp_path, installs):
    text = launcher()
    # Preserve the real delayed-expansion setting and subroutine bodies.
    header = "\n".join(text.splitlines()[:2]) + '\nset "PY="\n'
    header += 'call :check "%TEST_ROOT%"\nif not defined PY exit /b 1\necho FOUND\nexit /b 0\n'
    completed = run_branch(tmp_path, header + text[text.index(":check\n"):],
                           installs["nested"].parent.parent)
    assert completed.returncode == 0, "nested runtime no longer discovered"
    assert b"FOUND" in completed.stdout
