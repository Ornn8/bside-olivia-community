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
    assert SETTINGS_UI_VERSION == "p03.original-settings-manage.v7"
    for declaration in (
            'const STATUS_PATH = "/toy/companion/status";',
            'const MEMORY_PATH = "/toy/companion/memory";',
        'const MEMORY_CORRECT_PATH = "/toy/companion/memory/correct";',
        'const MEMORY_DELETE_PATH = "/toy/companion/memory/delete";',
        'const MEMORY_CLEAR_PATH = "/toy/companion/memory/clear";',
        'const MEMORY_PAUSE_PATH = "/toy/companion/memory/pause";',
        'const MEMORY_RESUME_PATH = "/toy/companion/memory/resume";',
        'const CONFIRM_HEADER = "X-Olivia-Companion-Action";',
        'const CONFIRM_VALUE = "confirmed";',
    ):
        assert BOOTSTRAP_JAVASCRIPT.count(declaration) == 1
    assert BOOTSTRAP_JAVASCRIPT.count('method: "GET"') == 2
    assert BOOTSTRAP_JAVASCRIPT.count('method: "POST"') == 2
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
    assert "crypto.randomUUID" in BOOTSTRAP_JAVASCRIPT
    assert 'element.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT
    assert 'element.style.webkitAppRegion = "no-drag";' in BOOTSTRAP_JAVASCRIPT
    assert 'backdrop.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT
    assert 'dialog.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT
    assert 'panel.style.display = active ? "grid" : "none";' in BOOTSTRAP_JAVASCRIPT
    assert '-webkit-app-region: no-drag !important;' in BOOTSTRAP_JAVASCRIPT
    assert 'var(--el-text-color-primary, #303133)' in BOOTSTRAP_JAVASCRIPT


def test_original_settings_can_apply_a_user_downloaded_patch_and_roll_back() -> None:
    source = BOOTSTRAP_JAVASCRIPT

    assert 'const UPDATE_ACTION_PATH = "/toy/updates/local/action";' in source
    assert 'action: "select"' in source
    assert "选择已下载的补丁" in source
    assert "payload.package_path" in source
    assert "File.path" not in source
    assert "patch.files" not in source
    assert "发布页提供的 Manifest SHA-256" in source
    assert 'action: "apply"' in source
    assert 'action: "rollback"' in source
    assert "安装本地补丁" in source
    assert "回滚上一版本" in source
    assert "关闭并重新打开 Olivia 后生效" in source


def test_original_settings_can_import_official_text_reply_history() -> None:
    assert BOOTSTRAP_JAVASCRIPT.count(
        'const OFFICIAL_LETTER_IMPORT_PATH = "/toy/letter/legacy/official-import";'
    ) == 1
    assert "导入官方文字信件" in BOOTSTRAP_JAVASCRIPT
    assert "只导入原信和文字回信，不导入视频" in BOOTSTRAP_JAVASCRIPT
    assert "requestMutation(OFFICIAL_LETTER_IMPORT_PATH, {})" in BOOTSTRAP_JAVASCRIPT
    assert "path === OFFICIAL_LETTER_IMPORT_PATH ? 600000 : 8000" in BOOTSTRAP_JAVASCRIPT
    assert "payload.inserted" in BOOTSTRAP_JAVASCRIPT
    assert "payload.memory_migration" in BOOTSTRAP_JAVASCRIPT
    assert "记忆已按时间顺序处理" in BOOTSTRAP_JAVASCRIPT


def test_official_import_uses_visible_confirmation_and_refreshes_legacy_list() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    harness = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(0, "utf8");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.textContent = "";
    this.className = "";
    this.disabled = false;
  }
  append(...items) {
    for (const item of items) {
      item.parentElement = this;
      this.children.push(item);
    }
  }
  replaceChildren(...items) {
    this.children = [];
    this.append(...items);
  }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((item) => item !== this);
    this.parentElement = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  async click() { return this.listeners.click && this.listeners.click({ target: this }); }
  focus() {}
  matches(selector) {
    if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1));
    if (selector.startsWith("[")) {
      const match = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
      return Boolean(match) && Object.prototype.hasOwnProperty.call(this.attributes, match[1])
        && (!match[2] || this.attributes[match[1]] === match[2]);
    }
    return this.tagName === selector;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(",").map((item) => item.trim());
    const found = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (selectors.some((item) => child.matches(item))) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const body = new Element("body");
const main = new Element("main");
const settings = new Element("section");
settings.className = "tp-settings-item";
const container = new Element("div");
container.append(settings);
main.append(container);
body.append(main);
const document = {
  body,
  documentElement: new Element("html"),
  currentScript: { dataset: { apiBase: "http://127.0.0.1:8899" } },
  createElement: (tag) => new Element(tag),
  querySelectorAll: (selector) => body.querySelectorAll(selector),
  querySelector: (selector) => body.querySelector(selector),
};
let nativeConfirmCalls = 0;
const calls = [];
const fetch = async (endpoint, options) => {
  calls.push({ path: endpoint.pathname, method: options.method, headers: options.headers });
  if (endpoint.pathname === "/toy/setup/status") {
    return { ok: true, json: async () => ({ status: "READY", setup_completed: true, show_initial_setup: false }) };
  }
  if (endpoint.pathname === "/toy/settings/video-reply") {
    return { ok: true, json: async () => ({ code: 0, data: { state: "available", enabled: false } }) };
  }
  if (endpoint.pathname === "/toy/letter/list") {
    return { ok: true, json: async () => ({ code: 0, data: {
      list: [{ letter_id: "legacy-1", summary: "legacy-summary", created_at: "2026-08-27T00:00:00Z" }],
      total: 1, scope: "legacy", read_only: true,
    } }) };
  }
  if (endpoint.pathname === "/toy/letter/legacy/official-import") {
    return { ok: true, json: async () => ({ code: 0, data: {
      status: "APPLIED", inserted: 1, duplicates: 0,
      memory_migration: { status: "completed", processed: 1 },
    } }) };
  }
  throw new Error(`unexpected request: ${endpoint.pathname}`);
};
const window = {
  location: { pathname: "/collection", hash: "#/settings" },
  requestAnimationFrame: (callback) => callback(),
  addEventListener: () => {},
  setTimeout: (callback) => { callback(); return 1; },
  clearTimeout: () => {},
  confirm: () => { nativeConfirmCalls += 1; throw new Error("native confirmation is unusable"); },
};
const context = {
  URL, AbortController, document, window, fetch,
  MutationObserver: class { constructor() {} observe() {} },
};
vm.runInNewContext(source, context);
const flush = async () => { for (let index = 0; index < 8; index += 1) await Promise.resolve(); };
(async () => {
  await flush();
  const buttons = body.querySelectorAll("button");
  const importButton = buttons[buttons.length - 1];
  if (!importButton) throw new Error("official import button missing");
  if (!body.querySelectorAll("span").some((item) => item.textContent === "legacy-summary")) {
    throw new Error("persisted legacy letter was not rendered");
  }
  const importPending = importButton.click();
  const confirmDialog = body.querySelector("[data-olivia-companion-official-import-confirm]");
  if (!confirmDialog) throw new Error("visible official import confirmation missing");
  const confirmButton = confirmDialog.querySelectorAll("button")[1];
  if (!confirmButton || confirmButton.style.pointerEvents !== "auto") throw new Error("confirmation button is not actionable");
  await confirmButton.click();
  await importPending;
  await flush();
  const importCall = calls.find((item) => item.path === "/toy/letter/legacy/official-import");
  if (!importCall || importCall.headers["X-Olivia-Companion-Action"] !== "confirmed") throw new Error("official import confirmation header missing");
  if (nativeConfirmCalls !== 0) throw new Error("native confirmation was used");
  process.stdout.write(JSON.stringify({ legacyListRequests: calls.filter((item) => item.path === "/toy/letter/list").length }));
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
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
    assert json.loads(result.stdout.decode("utf-8")) == {"legacyListRequests": 2}


def test_original_settings_private_world_entry_is_limited_to_safe_status() -> None:
    source = BOOTSTRAP_JAVASCRIPT

    assert "私人世界状态" in source
    assert "原因代码" in source
    for forbidden in (
        "PRIVATE_WORLD_PATH",
        "CANDIDATES_PATH",
        "legacyPrivateWorldLabels",
        "legacyCandidateRoute",
        "encodeURIComponent",
        "待确认的关系建议",
        "批准",
        "拒绝",
        "本地世界线",
        "renderPrivateSummary",
        "renderCandidateList",
        "renderPrivateWorldPanel(\n          panels.privateWorld,\n          capabilities.private_world,",
    ):
        assert forbidden not in source


def test_original_settings_private_world_status_fails_closed() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    harness = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(0, "utf8");
source = source.replace(/\}\)\(\);\s*$/, `
  globalThis.renderPrivateWorldStatus = renderPrivateWorldPanel;
})();\n`);

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.style = {}; this.textContent = ""; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  addEventListener() {}
}

const document = {
  currentScript: { dataset: { apiBase: "http://127.0.0.1:8899" } },
  createElement: (tag) => new Element(tag),
  querySelectorAll: () => [],
  querySelector: () => null,
  documentElement: new Element("html"),
};
const context = {
  URL, AbortController, document,
  MutationObserver: class { constructor() {} observe() {} },
  window: { requestAnimationFrame: () => {}, addEventListener: () => {} },
};
vm.runInNewContext(source, context);
(async () => {
  const render = context.renderPrivateWorldStatus;
  const result = [];
  for (const value of [
    { state: "available" },
    { state: "disabled" },
    { state: "unavailable", reason_code: "PRIVATE_WORLD_STORAGE_UNAVAILABLE" },
    { state: "degraded", reason_code: "PRIVATE_WORLD_STORAGE_UNAVAILABLE" },
    { state: "unknown", reason_code: "PRIVATE_WORLD_STORAGE_UNAVAILABLE" },
    {}, null,
    { state: "unavailable", reason_code: "C:/private/world.sqlite3" },
  ]) {
    const panel = new Element("section");
    await render(panel, value);
    result.push(panel.children.map((item) => item.textContent));
  }
  process.stdout.write(JSON.stringify(result));
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
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
    rendered = json.loads(result.stdout.decode("utf-8"))
    assert [item[1] for item in rendered] == [
        "状态：可用", "状态：未启用", "状态：暂不可用", "状态：暂不可用",
        "状态：暂不可用", "状态：暂不可用", "状态：暂不可用", "状态：暂不可用",
    ]
    assert rendered[2][2] == "原因代码：PRIVATE_WORLD_STORAGE_UNAVAILABLE"
    assert rendered[3][2] == "原因代码：无"
    assert rendered[4][2] == "原因代码：无"
    assert rendered[7][2] == "原因代码：无"


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
        "0–100",
    ):
        assert forbidden not in source
    for required in (
            "memory_id",
            "replacement_text",
            "request_id",
            "纠正",
        "删除",
        "暂停长期记忆",
        "恢复长期记忆",
        "清空当前用户记忆",
    ):
        assert required in source
    assert "http://" not in source
    assert "setup.llm.api_key" not in source
    assert 'key.input.value = ""' in source
    assert "https://api.deepseek.com" in source
    assert "https://opencode.ai/zen/go/v1" in source


def test_original_settings_clear_memory_uses_two_explicit_confirmations() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    assert source.count('const clear = button("清空当前用户记忆"') == 1
    assert "确认清空当前用户的 Mem0 长期记忆？" in source
    assert "清空后无法恢复。原始信件和私人世界不会受影响，仍要继续吗？" in source
    assert "requestMutation(MEMORY_CLEAR_PATH" in source
    assert 'request_id: requestId("memory.clear")' in source
    assert "confirmed: true" in source


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


def test_memory_lifecycle_toggle_refreshes_status_and_renders_resume_in_same_dialog() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    harness = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(0, "utf8");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.textContent = "";
    this.className = "";
    this.hidden = false;
  }
  append(...items) {
    for (const item of items) {
      item.parentElement = this;
      this.children.push(item);
    }
  }
  replaceChildren(...items) {
    this.children = [];
    this.append(...items);
  }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((item) => item !== this);
    this.parentElement = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  async click() { return this.listeners.click && this.listeners.click({ target: this }); }
  focus() {}
  matches(selector) {
    if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1));
    if (selector.startsWith("[")) {
      const match = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
      return Boolean(match) && Object.prototype.hasOwnProperty.call(this.attributes, match[1])
        && (!match[2] || this.attributes[match[1]] === match[2]);
    }
    return this.tagName === selector;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(",").map((item) => item.trim());
    const found = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (selectors.some((item) => child.matches(item))) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches(selector)) return node;
      node = node.parentElement;
    }
    return null;
  }
}

const body = new Element("body");
const main = new Element("main");
const settings = new Element("section");
settings.className = "tp-settings-item";
const container = new Element("div");
container.append(settings);
main.append(container);
body.append(main);
const document = {
  body,
  documentElement: new Element("html"),
  currentScript: { dataset: { apiBase: "http://127.0.0.1:8899" } },
  createElement: (tag) => new Element(tag),
  querySelectorAll: (selector) => body.querySelectorAll(selector),
  querySelector: (selector) => body.querySelector(selector),
};
let statusIndex = 0;
let currentStatus = "READY";
const mutationPaths = [];
const videoMethods = [];
let videoWrites = 0;
const statuses = ["READY", "PAUSED", "READY"];
const statusPayload = (status) => ({
  status,
  capabilities: {
    memory: status === "UNAVAILABLE"
      ? { state: "unavailable", reason_code: "MEMORY_ADMIN_CLEAR_PENDING" }
      : status === "PAUSED"
      ? { state: "degraded", reason_code: "MEMORY_ADMIN_PAUSED", count: 0 }
      : { state: "available", count: 0 },
    private_world: { state: "available" },
    candidates: { state: "available" },
  },
});
    const fetch = async (endpoint, options) => {
      if (endpoint.pathname === "/toy/setup/status") {
        return { ok: true, json: async () => ({
          status: "READY",
          setup_completed: true,
          show_initial_setup: false,
          llm: {
            base_url: "https://api.deepseek.com",
            model: "deepseek-v4-flash",
            key_configured: false,
          },
          }) };
      }
      if (endpoint.pathname === "/toy/capabilities/mem0") {
        return { ok: true, json: async () => ({
          status: "READY",
          capability: "long_term_memory",
          state: "missing",
          phase: "idle",
          downloaded_bytes: 0,
          total_bytes: 337000000,
          remaining_bytes: 337000000,
          installed_bytes: 0,
          install_locations: [
            { root: "installation_root", relative_path: "runtime/mem0-site-packages" },
            { root: "local_data_root", relative_path: "memory/model-cache" },
          ],
          version: "fixture",
          license_summary: "fixture",
          requires_gpu: false,
          }) };
      }
      if (endpoint.pathname === "/toy/letter/list") {
        return { ok: true, json: async () => ({ code: 0, data: {
          list: [], total: 0, scope: "legacy", read_only: true,
        } }) };
      }
      if (endpoint.pathname === "/toy/settings/video-reply") {
    videoMethods.push(options.method);
    if (options.method === "GET") return { ok: true, json: async () => ({ code: 0, data: { state: "available", enabled: true } }) };
    videoWrites += 1;
    return videoWrites === 1
      ? { ok: true, json: async () => ({ code: 0, data: { status: "APPLIED", enabled: false } }) }
      : { ok: false, json: async () => ({ data: { error_code: "VIDEO_REPLY_SETTING_UNAVAILABLE" } }) };
  }
  if (endpoint.pathname === "/toy/companion/status") {
    currentStatus = mutationPaths.length === 3
      ? "UNAVAILABLE"
      : mutationPaths.length > 3
      ? "READY"
      : statuses[Math.min(statusIndex++, statuses.length - 1)];
    return { ok: true, json: async () => statusPayload(currentStatus) };
  }
  if (endpoint.pathname === "/toy/companion/memory") {
    return { ok: true, json: async () => ({ status: currentStatus, memories: [] }) };
  }
  if (endpoint.pathname === "/toy/companion/private-world") {
    return { ok: true, json: async () => ({ status: currentStatus, levels: {} }) };
  }
  if (endpoint.pathname === "/toy/companion/private-world/candidates") {
    return { ok: true, json: async () => ({ status: currentStatus, candidates: [] }) };
  }
  mutationPaths.push(endpoint.pathname);
  return { ok: true, json: async () => ({ status: "APPLIED", request_id: "memory.lifecycle.1", affected_count: 0 }) };
};
const window = {
  location: { pathname: "/collection", hash: "#/settings" },
  requestAnimationFrame: (callback) => callback(),
  addEventListener: () => {},
  setTimeout: () => 1,
  clearTimeout: () => {},
  confirm: () => true,
};
const flush = async () => {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
};
const context = {
  URL, AbortController, crypto: { randomUUID: () => "fixture-uuid" },
  MutationObserver: class { constructor() {} observe() {} },
  document, window, fetch,
};
vm.runInNewContext(source, context);
(async () => {
  const findButton = (label) => body.querySelectorAll("button").find((item) => item.textContent === label);
  const open = findButton("打开");
  if (!open) throw new Error(`open button missing: ${body.querySelectorAll("button").map((item) => item.textContent).join("|")}`);
      await open.click();
      await flush();
      await findButton("已开启").click();
      await flush();
      await findButton("已关闭").click();
      await flush();
      if (!body.querySelectorAll("div").some((item) => item.textContent.includes("原设置保持不变"))) throw new Error("video mutation error was hidden");
      await findButton("暂停长期记忆").click();
  await flush();
  const resume = findButton("恢复长期记忆");
  if (!resume) throw new Error("resume button was not rendered after pause");
  await resume.click();
  await flush();
  const pause = findButton("暂停长期记忆");
  if (!pause) throw new Error("pause button was not rendered after resume");
  await pause.click();
  await flush();
  const pending = findButton("继续完成清空");
  if (!pending) throw new Error("pending clear recovery was not rendered");
  await pending.click();
  await flush();
  if (!findButton("暂停长期记忆")) throw new Error("clear recovery did not refresh status");
  process.stdout.write(JSON.stringify({ mutationPaths, videoMethods, statusIndex }));
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
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
    assert json.loads(result.stdout.decode("utf-8")) == {
        "mutationPaths": [
            "/toy/companion/memory/pause",
            "/toy/companion/memory/resume",
            "/toy/companion/memory/pause",
            "/toy/companion/memory/clear",
        ],
        "videoMethods": ["GET", "POST", "POST"],
        "statusIndex": 3,
    }


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
