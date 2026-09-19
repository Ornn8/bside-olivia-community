import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

import video_capability_install as installer


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths")
@pytest.mark.parametrize("root", [Path("C:/Olivia"), Path("//server/share/Olivia"), Path("//?/C:/Olivia")])
def test_breeze_pip_target_bypasses_windows_max_path(root, monkeypatch):
    target = root / ".staging" / ("ordinary_video-" + "a" * 32) / "breeze/runtime/Lib/site-packages"

    def run(command, **kwargs):
        value = command[command.index("--target") + 1]
        assert value.startswith("\\\\?\\")
        assert Path(value).name == "site-packages"
        expected = str(target)
        if expected.startswith("\\\\?\\"):
            assert value == expected
        elif expected.startswith("\\\\"):
            assert value == "\\\\?\\UNC\\" + expected[2:]
        else:
            assert value == "\\\\?\\" + expected
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.VideoCapabilityInstaller._install_breeze_runtime_packages(
        root / "breeze/runtime/python.exe", target, root / "requirements.txt"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths")
def test_breeze_installs_synthetic_offline_wheel_beyond_max_path(tmp_path):
    if importlib.util.find_spec("pip") is None:
        pytest.skip("Test interpreter has no pip")
    runtime = tmp_path / ("staging-" + "a" * 70) / "breeze/runtime"
    wheels = runtime.parent / "wheels"
    wheels.mkdir(parents=True)
    target = runtime / "Lib/site-packages"
    relative = "probe_package/" + "nested_" * 10 + "/" + "module_" * 10 + ".py"
    assert len(str(target / relative)) > 260
    dist_info = "olivia_longpath_probe-0.0.0.dist-info"
    payload = {
        relative: b"VALUE = 7\n",
        f"{dist_info}/METADATA": b"Metadata-Version: 2.1\nName: olivia-longpath-probe\nVersion: 0.0.0\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = f"{dist_info}/RECORD"
    payload[record] = "".join(f"{name},,\n" for name in [*payload, record]).encode()
    wheel = wheels / "olivia_longpath_probe-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "olivia-longpath-probe==0.0.0 --hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
        encoding="utf-8",
    )
    installer.VideoCapabilityInstaller._install_breeze_runtime_packages(
        runtime / "python.exe", target, requirements
    )
    installed = Path("\\\\?\\" + str(target.resolve()))
    assert (installed / relative).read_bytes() == payload[relative]
    assert list((installed / relative).parent.glob("__pycache__/*.pyc"))
