import hashlib

import pytest


@pytest.fixture
def synthetic_native_compiler(monkeypatch):
    """Package fixtures use synthetic assets; real csc is tested separately."""
    def build(source, output, **kwargs):
        content = b"MZ-synthetic-native-launcher"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        output.with_suffix(".exe.sha256").write_text(hashlib.sha256(content).hexdigest())
        return output
    monkeypatch.setattr("installer.build_native_launcher.build_native_launcher", build)
    return build
