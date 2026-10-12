"""Startup onboarding: no key -> account page first; bundled memory -> no wizard.

The injected bootstrap script runs in a small fake DOM under Node. The setup poll
is driven by hand (`__poll`) so every scenario is deterministic.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT

HARNESS = r'''
const fs = require("fs");
const vm = require("vm");
const assert = require("assert/strict");

let source = fs.readFileSync(0, "utf8");
source = source.replace(/\}\)\(\);\s*$/, `
  globalThis.__poll = maybeOpenInitialSetup;
})();\n`);

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.textContent = "";
    this.className = "";
    this.hidden = false;
    this.disabled = false;
  }
  append(...items) {
    for (const item of items) {
      if (item.parentElement) item.remove();
      item.parentElement = this;
      this.children.push(item);
    }
  }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((item) => item !== this);
    this.parentElement = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  async click() { for (const handler of this.listeners.click || []) await handler({ target: this }); }
  focus() {}
  get textAll() { return this.textContent + this.children.map((child) => child.textAll).join(""); }
  matches(selector) {
    if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1));
    if (selector.startsWith("[")) {
      const match = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
      return Boolean(match) && Object.hasOwn(this.attributes, match[1])
        && (match[2] === undefined || this.attributes[match[1]] === match[2]);
    }
    return /^[a-z0-9]+$/i.test(selector) && this.tagName === selector;
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
body.append(main);
const document = {
  body,
  documentElement: new Element("html"),
  currentScript: { dataset: { apiBase: "http://127.0.0.1:8899" } },
  createElement: (tag) => new Element(tag),
  querySelectorAll: (selector) => body.querySelectorAll(selector),
  querySelector: (selector) => body.querySelector(selector),
};

// What the fake local service answers; scenarios edit it before loading.
const config = JSON.parse(process.argv[1]);
const calls = [];
const token = "t".repeat(43);
const reply = (payload, ok = true) => ({ ok, json: async () => payload });
const fetch = async (endpoint, options = {}) => {
  const path = endpoint.pathname;
  const request = options.body ? JSON.parse(options.body) : null;
  calls.push({ path, method: options.method || "GET", body: request });
  if (path === "/toy/setup/status") {
    if (config.statusFailures > 0) { config.statusFailures -= 1; return reply({}, false); }
    return reply({
      status: "READY", schema_version: "olivia.initial-setup.v1", login_observed: true,
      setup_completed: config.completed, show_initial_setup: !config.completed, skipped: false,
      llm: { provider: "relay", base_url: "https://example.invalid/v1", model: "m", max_retries: 1,
             key_configured: config.key },
      session_token: token,
    });
  }
  if (path === "/toy/capabilities/mem0") {
    if (config.memory === "error") return reply({}, false);
    return reply({ status: "READY", capability: "long_term_memory", state: config.memory,
                   total_bytes: 1, remaining_bytes: 0, installed_bytes: 1, requires_gpu: false,
                   install_locations: [] });
  }
  if (path === "/toy/companion/status") {
    return reply({ status: "READY", capabilities: { memory: { state: "available" } } });
  }
  if (path === "/toy/setup/complete") {
    if (config.completeFails) return reply({ error_code: "LLM_SETUP_MEMORY_PREPARING" }, false);
    config.completed = true;
    return reply({ status: "READY", setup_completed: true });
  }
  if (path === "/toy/relay/action") {
    return reply(request && request.action === "account"
      ? { configured: config.key, registration_pending: false } : {});
  }
  return reply({}, false);
};

let timerId = 0;
const window = {
  location: { pathname: "/collection", hash: "#/" },
  requestAnimationFrame: (callback) => callback(),
  addEventListener: () => {},
  setTimeout: () => ++timerId,
  clearTimeout: () => {},
};
const context = {
  URL, AbortController, document, window, fetch,
  MutationObserver: class { constructor() {} observe() {} },
};
const unhandled = [];
process.on("unhandledRejection", (error) => unhandled.push(String(error && error.stack || error)));
vm.runInNewContext(source, context);

const settle = async () => { for (let i = 0; i < 4; i += 1) await new Promise((r) => setImmediate(r)); };
const poll = async () => { await context.__poll(); await settle(); };
const dialog = () => body.querySelector("[data-olivia-companion-settings-dialog]");
const heading = () => {
  const root = dialog();
  const node = root && root.querySelector("h2");
  return node ? node.textContent : null;
};
const isAccountPage = () => Boolean(dialog() && dialog().querySelector("[data-olivia-relay-dialog]"));
const labels = () => (dialog() ? dialog().querySelectorAll("button").map((item) => item.textContent) : []);
const selectedTab = () => {
  const tab = dialog() && dialog().querySelectorAll('[role="tab"]')
    .find((item) => item.attributes["aria-selected"] === "true");
  return tab ? tab.textContent : null;
};
const closeDialog = async () => {
  await dialog().querySelectorAll("button").find((item) => item.textContent === "关闭").click();
  assert.equal(dialog(), null);
};
const completeCalls = () => calls.filter((item) => item.path === "/toy/setup/complete");
const run = (scenario) => (async () => {
  await settle();
  const result = await scenario();
  assert.deepEqual(unhandled, []);
  process.stdout.write(JSON.stringify(result === undefined ? "ok" : result));
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''


def run_scenario(config: dict, scenario: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    script = HARNESS + "\nrun(async () => {\n" + scenario + "\n});\n"
    result = subprocess.run(
        [node, "-e", script, json.dumps(config)],
        input=BOOTSTRAP_JAVASCRIPT.encode("utf-8"),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return json.loads(result.stdout.decode("utf-8"))


def config(*, key: bool, completed: bool = False, memory: str = "missing", **extra) -> dict:
    return {"key": key, "completed": completed, "memory": memory, "completeFails": False,
            "statusFailures": 0, **extra}


@pytest.mark.parametrize("completed", [False, True])
def test_start_without_key_opens_the_account_page_once_per_session(completed):
    # Also when setup was completed or skipped earlier: the account page comes
    # first, closing it never brings it back, and the old poll goes on after it.
    out = run_scenario(config(key=False, completed=completed, memory="missing"), r'''
  assert.equal(isAccountPage(), true);
  assert.equal(heading(), "账户");
  assert.ok(labels().includes("获取 Key"));
  // The folded import section inside the account page has no second claim button.
  assert.ok(!labels().includes("去领取 Key"));
  const first = body.querySelectorAll("[data-olivia-companion-settings-dialog]").length;
  await poll(); await poll();           // dialog open: the poll must not stack another one
  assert.equal(body.querySelectorAll("[data-olivia-companion-settings-dialog]").length, first);
  await closeDialog();
  await poll(); await poll(); await poll();
  return { dialogs: body.querySelectorAll("[data-olivia-companion-settings-dialog]").length,
           accountAgain: isAccountPage(), wizard: heading() };
''')
    assert out["accountAgain"] is False
    assert out["dialogs"] == (0 if completed else 1)
    assert out["wizard"] == (None if completed else "欢迎使用 Olivia")


def test_start_with_key_does_not_open_the_account_page():
    out = run_scenario(config(key=True, completed=True, memory="ready"), r'''
  await poll(); await poll();
  return { dialog: dialog() !== null, complete: completeCalls().length };
''')
    assert out == {"dialog": False, "complete": 0}


def test_account_page_waits_for_a_reachable_service_and_never_stacks_on_a_dialog():
    out = run_scenario(config(key=False, completed=True, statusFailures=2), r'''
  assert.equal(dialog(), null);          // service still starting: nothing yet, no crash
  await poll();
  assert.equal(dialog(), null);
  await poll();
  assert.equal(isAccountPage(), true);   // first successful answer decides, once
  await closeDialog();
  await poll(); await poll();
  return { dialog: dialog() !== null };
''')
    assert out == {"dialog": False}


def test_a_key_deleted_later_in_the_session_does_not_reopen_the_account_page():
    out = run_scenario(config(key=True, completed=True), r'''
  await poll();
  config.key = false;                    // the user removes the key during this session
  await poll(); await poll();
  return { dialog: dialog() !== null };
''')
    assert out == {"dialog": False}


def test_wizard_connect_tab_offers_claim_button_that_opens_the_account_page():
    out = run_scenario(config(key=False, completed=False, memory="missing"), r'''
  await closeDialog();                   // dismiss the account page shown first
  await poll();
  assert.equal(heading(), "欢迎使用 Olivia");
  const claim = dialog().querySelectorAll("button").find((item) => item.textContent === "去领取 Key");
  assert.ok(claim, "claim button missing in the connect panel");
  assert.ok(dialog().querySelectorAll("input").length >= 1);   // import field stays
  assert.ok(labels().includes("连接并保存"));
  await claim.click();
  assert.equal(isAccountPage(), true);
  assert.equal(body.querySelectorAll("[data-olivia-companion-settings-dialog]").length, 1);
  return "ok";
''')
    assert out == "ok"


def test_wizard_connect_tab_has_no_claim_button_when_a_key_exists():
    out = run_scenario(config(key=True, completed=False, memory="missing"), r'''
  await poll();
  assert.equal(heading(), "欢迎使用 Olivia");
  assert.ok(!labels().includes("去领取 Key"));
  assert.ok(dialog().textAll.includes("已找到保存的 Key"));
  return "ok";
''')
    assert out == "ok"


def test_key_and_ready_memory_finish_setup_without_the_wizard():
    out = run_scenario(config(key=True, completed=False, memory="ready"), r'''
  assert.equal(dialog(), null);
  assert.deepEqual(completeCalls().map((item) => item.body), [{ skipped: false }]);
  assert.equal(window.location.hash, "#/collection");
  await poll(); await poll();
  return { dialog: dialog() !== null, complete: completeCalls().length };
''')
    assert out == {"dialog": False, "complete": 1}


@pytest.mark.parametrize("memory", ["missing", "repair", "paused", "downloading", "error"])
def test_key_without_ready_memory_keeps_the_wizard_on_the_import_tab(memory):
    out = run_scenario(config(key=True, completed=False, memory=memory), r'''
  assert.equal(heading(), "欢迎使用 Olivia");
  assert.equal(completeCalls().length, 0);
  return selectedTab();
''')
    assert out == "1 · 导入记忆包"


def test_failed_silent_finish_falls_back_to_the_wizard_on_the_connect_tab():
    out = run_scenario(config(key=True, completed=False, memory="ready", completeFails=True), r'''
  assert.equal(completeCalls().length, 1);
  assert.equal(heading(), "欢迎使用 Olivia");
  return selectedTab();
''')
    assert out == "2 · 连接回信服务"


def test_ready_memory_without_key_starts_the_wizard_on_the_connect_tab():
    out = run_scenario(config(key=False, completed=False, memory="ready"), r'''
  await closeDialog();                   // account page first
  await poll();
  assert.equal(completeCalls().length, 0);
  assert.equal(heading(), "欢迎使用 Olivia");
  return selectedTab();
''')
    assert out == "2 · 连接回信服务"


def test_claim_then_close_finishes_setup_silently_when_the_pack_is_ready():
    out = run_scenario(config(key=False, completed=False, memory="ready"), r'''
  assert.equal(isAccountPage(), true);
  config.key = true;                     // the key was claimed on the account page
  await closeDialog();
  await poll();
  return { dialog: dialog() !== null, complete: completeCalls().length, hash: window.location.hash };
''')
    assert out == {"dialog": False, "complete": 1, "hash": "#/collection"}
