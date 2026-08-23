from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_original_settings_bootstrap_has_valid_javascript(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for JavaScript syntax validation")
    source = tmp_path / "olivia-companion-settings.js"
    source.write_text(BOOTSTRAP_JAVASCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [node, "--check", str(source)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_original_settings_actions_remain_bounded_and_in_client() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    assert source.count('method: "POST"') == 1
    assert source.count('method: "GET"') == 1
    assert '"Content-Type": "application/json"' in source
    assert 'const CONFIRM_VALUE = "confirmed"' in source
    assert "window.confirm" in source
    assert "window.open" not in source
    assert "innerHTML" not in source
    assert "document.write" not in source
    assert "eval(" not in source
    assert "Function(" not in source
    assert "<iframe" not in source.casefold()
    assert 'method: "DELETE"' not in source
    assert 'method: "PUT"' not in source
