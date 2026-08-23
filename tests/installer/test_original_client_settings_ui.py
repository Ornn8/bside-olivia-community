from __future__ import annotations

import shutil
import subprocess

import pytest

from original_client_settings_ui import (
    BOOTSTRAP_JAVASCRIPT,
    SETTINGS_UI_VERSION,
)


def test_original_settings_read_ui_has_fixed_bounded_contract() -> None:
    assert SETTINGS_UI_VERSION == "p03.original-settings-read.v1"
    for path in (
        "/toy/companion/status",
        "/toy/companion/memory",
        "/toy/companion/private-world",
        "/toy/companion/private-world/candidates",
    ):
        assert BOOTSTRAP_JAVASCRIPT.count(path) == 1
    assert 'method: "GET"' in BOOTSTRAP_JAVASCRIPT
    assert "limit: 50" in BOOTSTRAP_JAVASCRIPT
    assert "input.maxLength = 500" in BOOTSTRAP_JAVASCRIPT
    assert "Promise.allSettled" in BOOTSTRAP_JAVASCRIPT


def test_original_settings_read_ui_renders_untrusted_data_as_text_only() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    assert "textContent" in source
    assert "replaceChildren" in source
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "window.open",
        "<iframe",
        'method: "POST"',
        'method: "PUT"',
        'method: "PATCH"',
        'method: "DELETE"',
        "memory_id",
        "source_id",
        "user_id",
        "database_path",
        "api_key",
        "0–100",
        "批准",
        "拒绝",
    ):
        assert forbidden not in source
    assert "http://" not in source
    assert "https://" not in source


def test_original_settings_read_ui_javascript_is_parseable() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    result = subprocess.run(
        [node, "--check", "-"],
        input=BOOTSTRAP_JAVASCRIPT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
