import os
import sys

import pytest

from installer import pip_runtime
from installer.build_windows_setup import _is_release_file


@pytest.mark.parametrize("pip_exit,compile_ok,expected", [(0, True, 0), (0, False, 1), (2, True, 2)])
def test_runtime_compiles_only_after_successful_pip_install(monkeypatch, pip_exit, compile_ok, expected):
    target = "runtime/site-packages"
    monkeypatch.setattr(sys, "argv", ["pip_runtime.py", "install", "--target", target])
    calls = []

    def run_module(name, **kwargs):
        assert name == "pip" and sys.argv == ["pip", "install", "--target", target]
        raise SystemExit(pip_exit)

    def compile_dir(path, **kwargs):
        calls.append(path)
        return compile_ok

    monkeypatch.setattr(pip_runtime.runpy, "run_module", run_module)
    monkeypatch.setattr(pip_runtime.compileall, "compile_dir", compile_dir)
    assert pip_runtime.main() == expected
    assert calls == ([] if pip_exit else [os.path.normpath(target)])
    assert _is_release_file("installer/pip_runtime.py")
