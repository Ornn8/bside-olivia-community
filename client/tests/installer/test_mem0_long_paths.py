import os
from pathlib import Path
import threading

import pytest

from mem0_capability_install import ManagedMem0Runtime


@pytest.mark.skipif(os.name != "nt", reason="Windows path length restrictions")
def test_mem0_install_extends_child_temp_and_pip_paths_without_changing_location(
    tmp_path, monkeypatch,
):
    root = tmp_path / "Chosen Product With Spaces"
    python = root / "runtime/python/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture")
    (python.parent / "python312._pth").write_text("python312.zip\n.\n", encoding="utf-8")
    requirements = root / "local_backend/installer/requirements.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("fixture==1\n", encoding="utf-8")
    offline = root / "verified-offline"
    wheelhouse = offline / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    temporary = root / ".olivia-setup-is-TEST.tmp"
    temporary.mkdir()
    monkeypatch.setenv("TEMP", str(temporary))
    monkeypatch.setenv("TMP", str(temporary))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(temporary))
    calls = []

    def run(command, *, environment, **_kwargs):
        calls.append(command)
        for key in ("TEMP", "TMP", "TMPDIR"):
            assert environment[key].startswith("\\\\?\\")
            assert os.path.samefile(environment[key], temporary)
        target = command[command.index("--target") + 1]
        assert target.startswith("\\\\?\\")
        assert os.path.samefile(target, root / "runtime/mem0-site-packages.staging")
        links = command[command.index("--find-links") + 1]
        assert links.startswith("\\\\?\\") and os.path.samefile(links, wheelhouse)
        (Path(target) / "fixture.dist-info").mkdir()
        return 0

    layer = ManagedMem0Runtime(
        install_root=root, python_executable=python, requirements=requirements,
        sources=("https://mirror.example/simple", "https://official.example/simple"),
        download_bytes=1, runner=run,
        verifier=lambda runtime, _: (runtime / "fixture.dist-info").is_dir(),
    )
    layer.install(source_mode="offline", offline_root=offline,
                  pause_requested=threading.Event(), progress=lambda *_: None)
    assert layer.ready()
    assert len(calls) == 1 and "--no-index" in calls[0]
    assert os.environ["TEMP"] == str(temporary)
    assert os.environ["TMP"] == str(temporary)
    assert os.environ["TMPDIR"] == str(temporary)
    target = root / "runtime/mem0-site-packages"
    assert (target / "fixture.dist-info").is_dir()
    assert (python.parent / "python312._pth").read_text(encoding="utf-8").splitlines()[0] == str(target)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive and UNC path syntax")
@pytest.mark.parametrize("target,expected", [
    (Path("C:/Olivia/runtime/staging"), "\\\\?\\C:\\Olivia\\runtime\\staging"),
    (Path("//server/share/Olivia/staging"), "\\\\?\\UNC\\server\\share\\Olivia\\staging"),
    (Path("//?/C:/Olivia/staging"), "\\\\?\\C:\\Olivia\\staging"),
])
def test_mem0_pip_target_supports_drive_unc_and_already_extended_paths(target, expected):
    layer = object.__new__(ManagedMem0Runtime)
    layer.python_executable = Path("C:/Olivia/runtime/python.exe")
    layer.requirements = Path("C:/Olivia/backend/requirements.txt")
    layer.cache = Path("C:/Olivia/downloads/pip-cache")
    command = layer._command(target=target, source="https://pypi.org/simple", wheelhouse=None)
    assert command[command.index("--target") + 1] == expected
    assert command[command.index("--index-url") + 1] == "https://pypi.org/simple"
