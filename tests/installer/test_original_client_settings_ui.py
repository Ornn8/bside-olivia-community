from __future__ import annotations

import shutil
import subprocess

import pytest

from original_client_settings_ui import (
    BOOTSTRAP_JAVASCRIPT,
    SETTINGS_UI_VERSION,
)


def test_original_settings_management_ui_has_fixed_bounded_contract() -> None:
    assert SETTINGS_UI_VERSION == "p03.original-settings-manage.v1"
    for declaration in (
        'const STATUS_PATH = "/toy/companion/status";',
        'const MEMORY_PATH = "/toy/companion/memory";',
        'const PRIVATE_WORLD_PATH = "/toy/companion/private-world";',
        'const CANDIDATES_PATH = "/toy/companion/private-world/candidates";',
        'const MEMORY_CORRECT_PATH = "/toy/companion/memory/correct";',
        'const MEMORY_DELETE_PATH = "/toy/companion/memory/delete";',
        'const CONFIRM_HEADER = "X-Olivia-Companion-Action";',
        'const CONFIRM_VALUE = "confirmed";',
    ):
        assert BOOTSTRAP_JAVASCRIPT.count(declaration) == 1
    assert BOOTSTRAP_JAVASCRIPT.count('method: "GET"') == 1
    assert BOOTSTRAP_JAVASCRIPT.count('method: "POST"') == 1
    assert "limit: 50" in BOOTSTRAP_JAVASCRIPT
    assert "input.maxLength = 500" in BOOTSTRAP_JAVASCRIPT
    assert "Promise.allSettled" in BOOTSTRAP_JAVASCRIPT
    assert "window.confirm" in BOOTSTRAP_JAVASCRIPT
    assert "crypto.randomUUID" in BOOTSTRAP_JAVASCRIPT
    assert "encodeURIComponent" in BOOTSTRAP_JAVASCRIPT
    assert 'element.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT
    assert 'element.style.webkitAppRegion = "no-drag";' in BOOTSTRAP_JAVASCRIPT
    assert 'backdrop.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT
    assert 'dialog.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT
    assert 'panel.style.display = active ? "grid" : "none";' in BOOTSTRAP_JAVASCRIPT
    assert '-webkit-app-region: no-drag !important;' in BOOTSTRAP_JAVASCRIPT
    assert 'var(--el-text-color-primary, #303133)' in BOOTSTRAP_JAVASCRIPT


def test_original_settings_management_ui_renders_untrusted_data_as_text_only() -> None:
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
        'method: "PUT"',
        'method: "PATCH"',
        'method: "DELETE"',
        "source_id",
        "user_id",
        "database_path",
        "api_key",
        "0–100",
    ):
        assert forbidden not in source
    for required in (
        "memory_id",
        "replacement_text",
        "request_id",
        "批准",
        "拒绝",
        "纠正",
        "删除",
    ):
        assert required in source
    assert "http://" not in source
    assert "https://" not in source


def test_original_settings_management_ui_javascript_is_parseable() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    result = subprocess.run(
        [node, "--check", "-"],
        input=BOOTSTRAP_JAVASCRIPT.encode("utf-8"),
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = (result.stderr or result.stdout).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
