from __future__ import annotations

import shutil
import subprocess
import json

import pytest

from original_client_settings_ui import (
    BOOTSTRAP_JAVASCRIPT,
    SETTINGS_UI_VERSION,
)


def _render_ready_mem0_status(*, companion_state: str | None) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    companion_response = (
        "throw new Error('offline');"
        if companion_state is None
        else f'''return {{ ok: true, json: async () => ({{
          status: "READY",
          capabilities: {{ memory: {{ state: "{companion_state}" }} }},
        }}) }};'''
    )
    harness = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(0, "utf8");
source = source.replace(/\}\)\(\);\s*$/, `
  globalThis.renderMem0CapabilityStatus = renderMem0CapabilityPanel;
})();\n`);

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.style = {};
    this.textContent = "";
    this.className = "";
    this.attributes = {};
    this.disabled = false;
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener() {}
}

const document = {
  currentScript: { dataset: { apiBase: "http://127.0.0.1:8899" } },
  createElement: (tag) => new Element(tag),
  querySelectorAll: () => [],
  querySelector: () => null,
  documentElement: new Element("html"),
};
const fetch = async (endpoint) => {
  if (endpoint.pathname === "/toy/capabilities/mem0") {
    return { ok: true, json: async () => ({
      status: "READY",
      capability: "long_term_memory",
      state: "ready",
      total_bytes: 332631647,
      remaining_bytes: 0,
      installed_bytes: 1015840112,
      requires_gpu: false,
      install_locations: [],
    }) };
  }
  if (endpoint.pathname === "/toy/companion/status") {
    __COMPANION_RESPONSE__
  }
  throw new Error(`unexpected request: ${endpoint.pathname}`);
};
const context = {
  URL, AbortController, document, fetch,
  MutationObserver: class { constructor() {} observe() {} },
  window: {
    location: { pathname: "/collection", hash: "" },
    requestAnimationFrame: () => {}, addEventListener: () => {},
    setTimeout: () => 1, clearTimeout: () => {},
  },
};
vm.runInNewContext(source, context);
(async () => {
  const panel = new Element("section");
  await context.renderMem0CapabilityStatus(panel);
  const metadata = panel.children[2];
  const statusRow = metadata && metadata.children[0];
  const statusValue = statusRow && statusRow.children[1];
  if (!statusValue) throw new Error("Mem0 status field was not rendered");
  process.stdout.write(statusValue.textContent);
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''.replace("__COMPANION_RESPONSE__", companion_response)
    result = subprocess.run(
        [node, "-e", harness],
        input=BOOTSTRAP_JAVASCRIPT.encode("utf-8"),
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = (result.stderr or result.stdout).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    return result.stdout.decode("utf-8")


def test_ready_mem0_reports_loaded_when_companion_runtime_is_available() -> None:
    assert _render_ready_mem0_status(companion_state="available") == "已安装并已加载"


def test_ready_mem0_requests_restart_when_companion_runtime_is_unavailable() -> None:
    assert _render_ready_mem0_status(companion_state="unavailable") == (
        "已安装，重启 Olivia 后加载"
    )


def test_ready_mem0_preserves_restart_fallback_when_companion_is_offline() -> None:
    assert _render_ready_mem0_status(companion_state=None) == (
        "已安装，重启 Olivia 后加载"
    )


def test_mem0_runtime_download_explains_temporarily_flat_progress() -> None:
    assert (
        '["python-runtime-preparation", "python-dependencies"].includes(payload.current_file)'
        in BOOTSTRAP_JAVASCRIPT
    )
    assert "mem0RuntimeProgressStartedAt = Date.now()" in BOOTSTRAP_JAVASCRIPT
    assert (
        "正在准备运行环境，已用时 ${runtimeElapsedSeconds} 秒（首次约需 3–8 分钟）"
        in BOOTSTRAP_JAVASCRIPT
    )


def test_memory_initialization_poll_recovers_and_stops_when_detached() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    harness = r'''
const fs = require("fs");
const source = fs.readFileSync(0, "utf8");
const body = source.split("  const scheduleMemoryStatusRefresh =")[1].split("\n\n  const renderMemoryPanel =")[0];
let timers = new Map(), nextId = 0, attempts = 0;
const window = { clearTimeout: (id) => timers.delete(id), setTimeout: (fn) => (timers.set(++nextId, fn), nextId) };
const requestJson = async () => { if (++attempts === 1) throw new Error("transient"); return { capabilities: { memory: {} } }; };
const renderMemoryPanel = async () => {}; const STATUS_PATH = "/status";
eval("var scheduleMemoryStatusRefresh =" + body);
const panel = { isConnected: true };
const run = async () => { const [id, fn] = timers.entries().next().value; timers.delete(id); await fn(); };
(async () => { scheduleMemoryStatusRefresh(panel); await run(); const failed = timers.size; await run(); const recovered = timers.size; scheduleMemoryStatusRefresh(panel); panel.isConnected = false; await run(); process.stdout.write(JSON.stringify({ failed, recovered, detached: timers.size })); })().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", harness], input=BOOTSTRAP_JAVASCRIPT.encode(), capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert json.loads(result.stdout) == {"failed": 1, "recovered": 0, "detached": 0}


# The shipped CEF surface needs explicit no-drag/pointer and display-state guards.
def test_original_settings_management_ui_has_fixed_bounded_contract() -> None:
    assert SETTINGS_UI_VERSION == "p03.original-settings-manage.v18"
    for declaration in (
            'const STATUS_PATH = "/toy/companion/status";',
            'const MEMORY_PATH = "/toy/companion/memory";',
        'const MEMORY_CORRECT_PATH = "/toy/companion/memory/correct";',
        'const MEMORY_DELETE_PATH = "/toy/companion/memory/delete";',
        'const MEMORY_CLEAR_PATH = "/toy/companion/memory/clear";',
        'const MEMORY_PAUSE_PATH = "/toy/companion/memory/pause";',
        'const MEMORY_RESUME_PATH = "/toy/companion/memory/resume";',
        'const MEMORY_RETRY_PATH = "/toy/companion/memory/retry";',
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
    assert "长期记忆正在准备，其他功能可正常使用" in BOOTSTRAP_JAVASCRIPT
    assert "重新准备长期记忆" in BOOTSTRAP_JAVASCRIPT
    assert '["INITIALIZING", "AVAILABLE"].includes(payload.status)' in BOOTSTRAP_JAVASCRIPT
    assert "window.confirm" not in BOOTSTRAP_JAVASCRIPT
    assert "const confirmAction = (message) => new Promise" in BOOTSTRAP_JAVASCRIPT
    assert 'confirmation.style.backgroundColor = "#18191c";' in BOOTSTRAP_JAVASCRIPT
    assert "crypto.randomUUID" in BOOTSTRAP_JAVASCRIPT
    assert 'element.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT
    assert 'element.style.webkitAppRegion = "no-drag";' in BOOTSTRAP_JAVASCRIPT
    assert 'backdrop.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT
    assert 'dialog.style.pointerEvents = "auto";' in BOOTSTRAP_JAVASCRIPT
    assert 'panel.style.display = active ? "grid" : "none";' in BOOTSTRAP_JAVASCRIPT
    assert '-webkit-app-region: no-drag !important;' in BOOTSTRAP_JAVASCRIPT
    assert 'dialog.style.backgroundColor = "#18191c";' in BOOTSTRAP_JAVASCRIPT
    assert 'dialog.style.color = "#f9fafb";' in BOOTSTRAP_JAVASCRIPT
    assert 'dialog.style.colorScheme = "dark";' in BOOTSTRAP_JAVASCRIPT
    assert 'color: #cbd5e1 !important;' in BOOTSTRAP_JAVASCRIPT
    assert '[role="dialog"] select' in BOOTSTRAP_JAVASCRIPT
    assert 'background-color: #111827 !important;' in BOOTSTRAP_JAVASCRIPT
    assert 'panel.style.background = "#202228";' in BOOTSTRAP_JAVASCRIPT
    assert 'element.style.background = "#2b2e35";' in BOOTSTRAP_JAVASCRIPT
    assert "var(--el-fill-color-light" not in BOOTSTRAP_JAVASCRIPT


def test_selected_settings_tab_keeps_a_distinct_visual_state() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    generic_controls = '[${DIALOG_ATTR}] [role="dialog"] button,'
    selected_tab = '[${DIALOG_ATTR}] [role="tab"][aria-selected="true"] {'
    unselected_tab = '[${DIALOG_ATTR}] [role="tab"][aria-selected="false"] {'

    assert selected_tab in source
    assert unselected_tab in source
    assert source.index(generic_controls) < source.index(selected_tab)
    assert "background-color: #374151 !important;" in source
    assert "tab.style.background =" not in source


def test_original_settings_can_apply_a_downloaded_patch_and_roll_back() -> None:
    source = BOOTSTRAP_JAVASCRIPT

    assert 'const UPDATE_ACTION_PATH = "/toy/updates/local/action";' in source
    assert "File.path" not in source
    assert "patch.files" not in source
    assert 'action: "select"' in source
    assert "选择已下载的补丁" in source
    assert "payload.package_path" in source
    assert "Manifest SHA-256" in source
    assert 'action: "apply"' in source
    assert 'action: "rollback"' in source
    assert "安装本地补丁" in source
    assert "回滚上一版本" in source
    assert "controls.append(choose, install, rollback);" in source
    assert "关闭并重新打开 Olivia 后生效" in source


def test_original_settings_imports_local_history_without_official_server() -> None:
    assert BOOTSTRAP_JAVASCRIPT.count(
        'const LOCAL_LETTER_IMPORT_PATH = "/toy/letter/legacy/local-import";'
    ) == 1
    assert "导入本地历史信件" in BOOTSTRAP_JAVASCRIPT
    assert "官方服务器已关闭" in BOOTSTRAP_JAVASCRIPT
    assert "letter_pairs.json" in BOOTSTRAP_JAVASCRIPT
    assert "作为只读历史进入信箱" in BOOTSTRAP_JAVASCRIPT
    assert "不联网" in BOOTSTRAP_JAVASCRIPT
    assert "requestMutation(LOCAL_LETTER_IMPORT_PATH, {})" in BOOTSTRAP_JAVASCRIPT
    assert "requestJson(LOCAL_LETTER_IMPORT_PATH)" in BOOTSTRAP_JAVASCRIPT
    assert "payload.inserted" in BOOTSTRAP_JAVASCRIPT
    assert "payload.updated" in BOOTSTRAP_JAVASCRIPT
    assert "payload.would_insert" in BOOTSTRAP_JAVASCRIPT
    assert "payload.would_update" in BOOTSTRAP_JAVASCRIPT
    assert "payload.would_remove" in BOOTSTRAP_JAVASCRIPT
    assert "未在原版游戏目录找到 letter_pairs.json" in BOOTSTRAP_JAVASCRIPT
    assert 'importButton.textContent = "重试导入"' in BOOTSTRAP_JAVASCRIPT
    assert "mountOfficialLetterImport(section)" not in BOOTSTRAP_JAVASCRIPT


def test_local_import_prompts_for_missing_backup_and_uses_visible_confirmation() -> None:
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
let backupAvailable = false;
const calls = [];
const fetch = async (endpoint, options) => {
  calls.push({ path: endpoint.pathname, method: options.method, headers: options.headers });
  if (endpoint.pathname === "/toy/setup/status") {
    return { ok: true, json: async () => ({ status: "READY", setup_completed: true, show_initial_setup: false }) };
  }
  if (endpoint.pathname === "/toy/settings/video-reply") {
    return { ok: true, json: async () => ({ code: 0, data: { state: "available", enabled: false } }) };
  }
  if (endpoint.pathname === "/toy/companion/status") {
    return { ok: true, json: async () => ({
      status: "READY",
      capabilities: {
        memory: { state: "available" },
        private_world: { state: "available" },
        candidates: { state: "available" },
      },
    }) };
  }
  if (endpoint.pathname === "/toy/letter/list") {
    return { ok: true, json: async () => ({ code: 0, data: {
      list: [{ letter_id: "legacy-1", summary: "legacy-summary", created_at: "2026-08-27T00:00:00Z" }],
      total: 1, scope: "legacy", read_only: true,
    } }) };
  }
  if (endpoint.pathname === "/toy/letter/legacy/local-import") {
    if (options.method === "GET") {
      return backupAvailable
        ? { ok: true, json: async () => ({ code: 0, data: {
            status: "READY", seen: 2, would_insert: 2, would_update: 0, would_remove: 0, duplicates: 0,
            source: "local_backup",
          } }) }
        : { ok: false, json: async () => ({ code: 404, data: {
            status: "UNAVAILABLE",
            error_code: "OFFLINE_LETTER_BACKUP_REQUIRED",
            retryable: true,
            source: "local_backup",
          } }) };
    }
    return { ok: true, json: async () => ({ code: 0, data: {
      status: "APPLIED", inserted: 2, updated: 0, removed: 0, duplicates: 0,
      source: "local_backup",
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
  if (!importButton) throw new Error("local import button missing");
  if (body.querySelectorAll("span").some((item) => item.textContent === "legacy-summary")) {
    throw new Error("history must be rendered in the mailbox, not settings");
  }
  await importButton.click();
  await flush();
  if (body.querySelector("[data-olivia-companion-official-import-confirm]")) {
    throw new Error("confirmation opened while backup was unavailable");
  }
  if (calls.some((item) => item.path === "/toy/letter/legacy/local-import" && item.method === "POST")) {
    throw new Error("local import wrote while backup was unavailable");
  }
  if (!body.querySelectorAll("div").some((item) => item.textContent.includes("官方服务器已关闭，请先准备本地备份"))) {
    throw new Error("missing backup prompt was not shown");
  }
  backupAvailable = true;
  const importPending = importButton.click();
  await flush();
  const confirmDialog = body.querySelector("[data-olivia-companion-official-import-confirm]");
  if (!confirmDialog) throw new Error("visible local import confirmation missing");
  const confirmButton = confirmDialog.querySelectorAll("button")[1];
  if (!confirmButton || confirmButton.style.pointerEvents !== "auto") throw new Error("confirmation button is not actionable");
  await confirmButton.click();
  await importPending;
  await flush();
  const importCall = calls.find((item) => item.path === "/toy/letter/legacy/local-import" && item.method === "POST");
  const preflightIndex = calls.findIndex((item) => item.path === "/toy/letter/legacy/local-import" && item.method === "GET");
  const importIndex = calls.findIndex((item) => item.path === "/toy/letter/legacy/local-import" && item.method === "POST");
  if (!importCall || importCall.headers["X-Olivia-Companion-Action"] !== "confirmed") throw new Error("local import confirmation header missing");
  if (preflightIndex < 0 || preflightIndex >= importIndex) throw new Error("local import preflight did not run before import");
  if (!body.querySelectorAll("div").some((item) => /2.*0.*0/.test(item.textContent))) {
    throw new Error("local import completion was not shown");
  }
  if (nativeConfirmCalls !== 0) throw new Error("native confirmation was used");
  process.stdout.write(JSON.stringify({
    legacyListRequests: calls.filter((item) => item.path === "/toy/letter/list").length,
    missingBackupPrompt: true,
    importPreflight: true,
    localImportCompleted: true,
  }));
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
        "legacyListRequests": 0,
        "missingBackupPrompt": True,
        "importPreflight": True,
        "localImportCompleted": True,
    }


def test_original_settings_private_world_entry_shows_character_life_not_scores() -> None:
    source = BOOTSTRAP_JAVASCRIPT

    assert "林离的生活" in source
    assert "原因代码" in source
    for forbidden in (
        "CANDIDATES_PATH",
        "legacyPrivateWorldLabels",
        "legacyCandidateRoute",
        "待确认的关系建议",
        "批准",
        "拒绝",
        "本地世界线",
        "renderPrivateSummary",
        "renderCandidateList",
        "renderPrivateWorldPanel(\n          panels.privateWorld,\n          capabilities.private_world,",
    ):
        assert forbidden not in source
    for required in (
        "PRIVATE_WORLD_PATH",
        "此刻的林离",
        "最近在忙",
        "生活片段",
        "与你有关",
        "DAILY_LIFE_PATH",
    ):
        assert required in source
    assert "fields.map(([key, label])" not in source


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
  fetch: async (url) => ({
    ok: true,
    json: async () => ({
      status: "READY",
          schema_version: "olivia.daily-life.v1", stale: false, refreshing: false,
          current: { location: "琴房", activity: "练琴", note: "慢慢弹稳。", occurred_at: "2026-09-05T10:00:00Z" },
          projects: [], shared: [], moments: [],
    }),
  }),
  MutationObserver: class { constructor() {} observe() {} },
  window: {
    requestAnimationFrame: () => {}, addEventListener: () => {},
    setTimeout: () => 1, clearTimeout: () => {},
  },
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
        "近况已保存。", "状态：未启用", "状态：暂不可用", "状态：暂不可用",
        "状态：暂不可用", "状态：暂不可用", "状态：暂不可用", "状态：暂不可用",
    ]
    assert rendered[2][2] == "原因代码：PRIVATE_WORLD_STORAGE_UNAVAILABLE"
    assert rendered[3][2] == "原因代码：无"
    assert rendered[4][2] == "原因代码：无"
    assert rendered[7][2] == "原因代码：无"
    assert rendered[0][0] == "林离的生活"
    assert rendered[0][2] == "更新近况"


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


def test_memory_list_refreshes_the_visible_count_after_mutations() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    assert "const updateSummary = (count) =>" in source
    assert "const latestStatus = await requestJson(STATUS_PATH);" in source
    assert "updateSummary(latestMemory && latestMemory.count);" in source


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
      if (endpoint.pathname === "/toy/companion/private-world/life") {
        return { ok: true, json: async () => ({ status: "READY", schema_version: "olivia.daily-life.v1", stale: false,
          current: null, projects: [], shared: [], moments: [], refreshing: false }) };
      }
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
      if (endpoint.pathname === "/toy/capabilities/video") {
        return { ok: true, json: async () => ({ status: "READY", bundles: [] }) };
      }
      if (endpoint.pathname === "/toy/capabilities/video/action") {
        return { ok: true, json: async () => ({ status: "APPLIED" }) };
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
      const clickConfirmed = async (target, count = 1) => {
        const pending = target.click();
        for (let index = 0; index < count; index += 1) {
          await flush();
          const dialogs = body.querySelectorAll('[role="dialog"]');
          const confirmation = dialogs[dialogs.length - 1];
          const buttons = confirmation && confirmation.querySelectorAll("button");
          if (!buttons || buttons.length < 2) throw new Error("confirmation dialog missing");
          await buttons[buttons.length - 1].click();
        }
        await pending;
      };
  const open = findButton("打开");
  if (!open) throw new Error(`open button missing: ${body.querySelectorAll("button").map((item) => item.textContent).join("|")}`);
      await open.click();
      await flush();
      await findButton("已开启").click();
      await flush();
      await findButton("已关闭").click();
      await flush();
      if (!body.querySelectorAll("div").some((item) => item.textContent.includes("原设置保持不变"))) throw new Error("video mutation error was hidden");
          await clickConfirmed(findButton("暂停长期记忆"));
  await flush();
  const resume = findButton("恢复长期记忆");
  if (!resume) throw new Error("resume button was not rendered after pause");
      await clickConfirmed(resume);
  await flush();
  const pause = findButton("暂停长期记忆");
  if (!pause) throw new Error("pause button was not rendered after resume");
      await clickConfirmed(pause);
  await flush();
  const pending = findButton("继续完成清空");
  if (!pending) throw new Error("pending clear recovery was not rendered");
      await clickConfirmed(pending, 2);
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
