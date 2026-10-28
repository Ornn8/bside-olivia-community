import json
import shutil
import subprocess

import pytest

from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT


@pytest.mark.parametrize("mode", ["mac-backend", "mac-native", "windows"])
def test_qq_settings_offer_supported_connection_and_save_manual_binding(tmp_path, mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for settings interaction test")
    harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.style = {};
    this.listeners = {}; this.classList = {add(){}}; this.isConnected = true;
    this.value = ''; this.ownText = '';
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  set textContent(value) { this.ownText = value; }
  get textContent() { return this.ownText + this.children.map(c => c.textContent).join(''); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute() {}
  querySelector() { return null; }
  contains() { return false; }
}
const mode = MODE;
const elements = [], posts = [], anchor = new Element('anchor');
const create = tag => { const el = new Element(tag); elements.push(el); return el; };
const status = {
  selected_channels: ['qq'], listeners: {qq: 'SETUP_REQUIRED'}, qq: {},
  napcat: {state: 'FAILED', error: 'NAPCAT_WINDOWS_REQUIRED', version: 'v4.18.28'},
  ...(mode === 'mac-backend' ? {managed_napcat_supported: false} : {}),
};
const context = {
  window: mode === 'mac-native' ? {olivia: {status: async () => ({managedNapcatSupported: false})}} : {},
  document: {
    currentScript: {dataset: {apiBase: 'http://127.0.0.1:1'}},
    createElement: create, createDocumentFragment: () => create('fragment'),
    querySelector: selector => selector.includes('group-body') ? anchor : null,
    head: new Element('head'), documentElement: new Element('html'),
  },
  Headers, URL, MutationObserver: class {observe(){}},
  setTimeout(){}, clearTimeout(){},
  fetch: async (url, options) => {
    if (options.method === 'POST') posts.push({path: url.pathname, body: JSON.parse(options.body)});
    return {ok: true, json: async () => structuredClone(status)};
  },
};
vm.runInNewContext(SOURCE, context);
setImmediate(async () => {
  try {
    const install = elements.find(el => el.tag === 'button' && el.textContent === '一键安装');
    if (mode === 'windows') {
      assert.ok(install, 'Windows install action disappeared');
      assert.ok(anchor.textContent.includes('Olivia 可以一键准备本地 QQ 组件'));
      assert.ok(!anchor.textContent.includes('macOS 使用'));
      return;
    }
    assert.equal(install, undefined, 'Mac exposed the Windows-only installer');
    assert.ok(anchor.textContent.includes('macOS 使用你自行运行的 NapCat / OneBot'));
    assert.ok(!anchor.textContent.includes('NAPCAT_WINDOWS_REQUIRED'));
    const manual = elements.find(el => el.tag === 'details');
    assert.equal(manual.open, true, 'the supported Mac connection form is hidden');
    const values = ['12345', '54321', 'ws://127.0.0.1:3001', 'synthetic-token-123456'];
    elements.filter(el => el.tag === 'input').forEach((el, index) => { el.value = values[index]; });
    const save = elements.find(el => el.tag === 'button' && el.textContent === '测试并保存');
    await save.listeners.click();
    assert.deepEqual(posts, [{path: '/toy/personal-chat/setup/qq/configure', body: {
      managed: false, account: values[0], owner: values[1], url: values[2], token: values[3],
    }}]);
    assert.equal(elements.find(el => el.type === 'password').value, '');
  } catch (error) { console.error(error); process.exitCode = 1; }
});
'''.replace("MODE", json.dumps(mode)).replace("SOURCE", json.dumps(PERSONAL_CHAT_SETUP_JAVASCRIPT))
    path = tmp_path / "qq-platform.cjs"
    path.write_text(harness, encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
