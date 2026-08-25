from __future__ import annotations

import shutil
import subprocess
import json

import pytest

from original_client_settings_ui import (
    BOOTSTRAP_JAVASCRIPT,
    SETTINGS_UI_VERSION,
)


# The shipped CEF surface needs explicit no-drag/pointer and display-state guards.
def test_original_settings_management_ui_has_fixed_bounded_contract() -> None:
    assert SETTINGS_UI_VERSION == "p03.original-settings-manage.v1"
    for declaration in (
        'const STATUS_PATH = "/toy/companion/status";',
        'const MEMORY_PATH = "/toy/companion/memory";',
        'const PRIVATE_WORLD_PATH = "/toy/companion/private-world";',
        'const CANDIDATES_PATH = "/toy/companion/private-world/candidates";',
        'const MEMORY_CORRECT_PATH = "/toy/companion/memory/correct";',
        'const MEMORY_DELETE_PATH = "/toy/companion/memory/delete";',
        'const MEMORY_PAUSE_PATH = "/toy/companion/memory/pause";',
        'const MEMORY_RESUME_PATH = "/toy/companion/memory/resume";',
        'const MEMORY_CLEAR_PATH = "/toy/companion/memory/clear";',
        'const CONFIRM_HEADER = "X-Olivia-Companion-Action";',
        'const CONFIRM_VALUE = "confirmed";',
    ):
        assert BOOTSTRAP_JAVASCRIPT.count(declaration) == 1
    assert BOOTSTRAP_JAVASCRIPT.count('method: "GET"') == 1
    assert BOOTSTRAP_JAVASCRIPT.count('method: "POST"') == 1
    assert "limit: 50" in BOOTSTRAP_JAVASCRIPT
    assert "input.maxLength = 500" in BOOTSTRAP_JAVASCRIPT
    assert "const LETTER_CHARACTER_LIMIT = 1200;" in BOOTSTRAP_JAVASCRIPT
    assert (
        "matches.values().next().value.maxLength = LETTER_CHARACTER_LIMIT;"
    ) in BOOTSTRAP_JAVASCRIPT
    assert 'const LETTER_COMPOSER_TITLE = "写下你的感受";' in BOOTSTRAP_JAVASCRIPT
    assert 'const LETTER_SUBMIT_LABEL = "寄出信件";' in BOOTSTRAP_JAVASCRIPT
    assert "Promise.allSettled" in BOOTSTRAP_JAVASCRIPT
    assert "window.confirm" in BOOTSTRAP_JAVASCRIPT
    assert "window.prompt" in BOOTSTRAP_JAVASCRIPT
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
        "暂停长期记忆",
        "恢复长期记忆",
        "清空所有长期记忆",
        "Archive 和私人世界不会被删除",
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


def test_letter_limit_handles_late_mount_and_fails_closed_on_ambiguous_dialogs() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    harness = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(0, "utf8");

const run = (spec) => {
  const sharedAreas = new Map();
  const makeDialog = (item) => {
    let areas = item.shared ? sharedAreas.get(item.shared) : null;
    if (!areas) {
      areas = Array.from({ length: item.textareas }, () => ({ maxLength: null }));
      if (item.shared) sharedAreas.set(item.shared, areas);
    }
    const nodes = (values) => values.map((textContent) => ({ textContent }));
    return {
      areas,
      closest: () => item.companion ? {} : null,
      querySelector: (selector) => selector === "textarea" ? areas[0] || null : null,
      querySelectorAll: (selector) => {
        if (selector === "textarea") return areas;
        if (selector === "button") return nodes(item.buttons);
        if (selector === 'h1,h2,h3,[class*="title"]') return nodes([item.title]);
        return [];
      },
    };
  };
  const dialogs = spec.initial.map(makeDialog);
  let mutationCallback = () => {};
  const document = {
    currentScript: { dataset: { apiBase: "http://127.0.0.1:8899" } },
    documentElement: {},
    querySelectorAll: (selector) => selector === '[role="dialog"], .el-dialog' ? dialogs : [],
    querySelector: () => null,
  };
  const context = {
    URL,
    document,
    window: {
      location: { pathname: "/collection", hash: "" },
      requestAnimationFrame: (callback) => callback(),
      addEventListener: () => {},
    },
    MutationObserver: class {
      constructor(callback) { mutationCallback = callback; }
      observe() {}
    },
  };
  vm.runInNewContext(source, context);
  for (const item of spec.late || []) dialogs.push(makeDialog(item));
  if (spec.late) mutationCallback();
  return dialogs.map((dialog) => dialog.areas.map((area) => area.maxLength));
};

const letter = {
  title: "写下你的感受",
  buttons: ["寄出信件"],
  textareas: 1,
  companion: false,
};
process.stdout.write(JSON.stringify([
  run({ initial: [], late: [letter] }),
  run({ initial: [{ ...letter, title: "其他弹窗" }] }),
  run({ initial: [letter, letter] }),
  run({ initial: [
    { ...letter, shared: "nested" },
    { ...letter, shared: "nested" },
  ] }),
  run({ initial: [letter, { ...letter, companion: true }] }),
]));
'''
    result = subprocess.run(
        [node, "-e", harness],
        input=BOOTSTRAP_JAVASCRIPT.encode("utf-8"),
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = (result.stderr or result.stdout).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    assert json.loads(result.stdout.decode("utf-8")) == [
        [[1200]],
        [[None]],
        [[None], [None]],
        [[1200], [1200]],
        [[1200], [None]],
    ]
