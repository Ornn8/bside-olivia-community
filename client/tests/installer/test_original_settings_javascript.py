from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_relationship_refresh_keeps_loaded_content_visible():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    source = BOOTSTRAP_JAVASCRIPT.split('const relationshipPanel =', 1)[1].split('const draw =', 1)[0]
    harness = r'''
const assert = require('node:assert/strict');
class Element {
  constructor(){this.children=[];this.style={};this.isConnected=true;this.open=true;this.events={};}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  addEventListener(name,handler){this.events[name]=handler;}
}
const document={createElement:()=>new Element()}, alive=()=>true;
const text=(tag,value)=>({textContent:value,style:{}}), button=()=>({});
const PRIVATE_WORLD_PATH='world';
let resolve, calls=0;
const requestJson=()=>{calls++;return new Promise(r=>resolve=r);};
'''
    harness += 'const relationshipPanel =' + source
    harness += r'''
(async()=>{
  const first=relationship.refresh();
  resolve({status:'READY',levels:{trust:'high'},relationship_stage:'friend'});
  await first;
  const content=relationship.children[1];
  const visible=content.children[0];
  const next=relationship.refresh();
  assert.equal(content.children[0],visible);
  relationship.events.toggle();
  assert.equal(calls,2);
  resolve({status:'READY',levels:{trust:'high'},relationship_stage:'friend'});
  await next;
  assert.ok(content.children.length>1);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    completed = subprocess.run([node, '-e', harness], capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stderr.decode('utf-8', errors='replace')


def test_memory_summary_tracks_latest_status_and_read_failure():
    from tests.installer.test_memory_search_order import run_memory_browser

    run_memory_browser(r'''
let fail=false,calls=0;
requestJson=async path=>{
  assert.equal(path,MEMORY_PATH);calls++;
  if(fail)throw Error('timeout');
  return {...page('visible-record'),total_count:7};
};
const panel=new Element('div');await renderMemoryPanel(panel,{state:'available',count:5});
const summary=panel.querySelector('.om-heading').querySelector('p');
assert.match(summary.textContent,/AVAILABLE.*7/);
fail=true;await panel.querySelectorAll('button').find(e=>e.textContent==='刷新 / 重试').click();await settle();
assert.match(summary.textContent,/UNAVAILABLE/);assert.doesNotMatch(summary.textContent,/[57]/);
assert.match(panel.querySelector('.om-result').textContent,/读取失败.*保留/);
assert.equal(panel.querySelector('.om-row-excerpt').textContent,'visible-record');
fail=false;await panel.querySelectorAll('button').find(e=>e.textContent==='刷新 / 重试').click();await settle();
assert.match(summary.textContent,/AVAILABLE.*7/);assert.equal(panel.querySelector('.om-result').textContent,'');
assert.equal(calls,3,'one page read per refresh, without an extra status probe');
''')


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
    assert source.count('method: "POST"') == 5  # + diagnostics save
    assert source.count('method: "GET"') == 2
    assert '"Content-Type": "application/json"' in source
    assert 'const CONFIRM_VALUE = "confirmed"' in source
    assert "window.confirm" not in source


def test_llm_save_and_delete_copy_says_changes_apply_immediately() -> None:
    source = BOOTSTRAP_JAVASCRIPT

    assert "已连接并保存 Olivia 回信服务，下一次发送生效。" in source
    assert "连接已删除。下一次发送立即生效。" in source
    assert "重启 Olivia 后生效" not in source
    assert "confirmAction" in source
    assert "window.open" not in source
    assert "innerHTML" not in source
    assert "document.write" not in source
    assert "eval(" not in source
    assert "Function(" not in source
    assert "<iframe" not in source.casefold()
    assert 'method: "DELETE"' not in source
    assert 'method: "PUT"' not in source


def test_confirmation_dialog_can_be_cancelled_by_backdrop_or_escape() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for confirmation behavior validation")
    harness = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(0, "utf8");
source = source.replace(/\s*schedule\(\);\s*\}\)\(\);\s*$/, `
  globalThis.confirmAction = confirmAction;
})();\n`);

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.parent = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || null; }
  addEventListener(name, listener) {
    (this.listeners[name] ||= []).push(listener);
  }
  append(...children) {
    for (const child of children) {
      child.parent = this;
      this.children.push(child);
    }
  }
  remove() {
    if (this.parent) {
      this.parent.children = this.parent.children.filter((child) => child !== this);
    }
  }
  focus() {}
}

const body = new Element("body");
const document = {
  currentScript: { dataset: { apiBase: "http://127.0.0.1:8899/" } },
  body,
  documentElement: body,
  createElement: (tag) => new Element(tag),
  querySelector: () => body.children[body.children.length - 1] || null,
};
const context = {
  URL,
  document,
  MutationObserver: class { observe() {} },
  window: { addEventListener: () => {} },
};
vm.runInNewContext(source, context);

(async () => {
  const backdropPending = context.confirmAction("确认删除？");
  const backdrop = body.children[0];
  const confirmation = backdrop.children[0];
  const message = confirmation.children[0];
  let clickPrevented = false;
  for (const listener of backdrop.listeners.click || []) {
    listener({ target: backdrop, preventDefault: () => { clickPrevented = true; } });
  }
  const backdropResult = await backdropPending;

  const escapePending = context.confirmAction("确认回滚？");
  const escapeBackdrop = body.children[0];
  let escapePrevented = false;
  for (const listener of escapeBackdrop.listeners.keydown || []) {
    listener({ key: "Escape", preventDefault: () => { escapePrevented = true; } });
  }
  const escapeResult = await escapePending;

  process.stdout.write(JSON.stringify({
    backdropResult,
    escapeResult,
    clickPrevented,
    escapePrevented,
    labelledBy: confirmation.getAttribute("aria-labelledby"),
    messageId: message.id || null,
    remainingBackdrops: body.children.length,
  }));
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
'''
    completed = subprocess.run(
        [node, "-e", harness],
        input=BOOTSTRAP_JAVASCRIPT.encode("utf-8"),
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
    assert completed.returncode == 0, output
    assert json.loads(completed.stdout) == {
        "backdropResult": False,
        "escapeResult": False,
        "clickPrevented": True,
        "escapePrevented": True,
        "labelledBy": "olivia-companion-confirm-message",
        "messageId": "olivia-companion-confirm-message",
        "remainingBackdrops": 0,
    }


def test_original_settings_reuses_llm_setup_after_login() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    assert 'const SETUP_STATUS_PATH = "/toy/setup/status"' in source
    assert "/toy/setup/llm/test" not in source
    assert "/toy/setup/llm/save" not in source
    assert "/toy/setup/llm/models" not in source
    assert 'const SETUP_COMPLETE_PATH = "/toy/setup/complete"' in source
    assert 'const LLM_DELETE_PATH = "/toy/setup/llm/delete"' in source
    assert 'const MEM0_CAPABILITY_PATH = "/toy/capabilities/mem0"' in source
    assert 'const MEM0_CAPABILITY_ACTION_PATH = "/toy/capabilities/mem0/action"' in source
    assert "/toy/capabilities/mem0/import" not in source
    assert "show_initial_setup" in source
    assert "Olivia Key" in source
    for retired in ("OpenCode Go", "DeepSeek", "dashscope", "自定义 OpenAI 兼容接口"):
        assert retired not in source
    assert "导入离线包（暂不可用）" not in source
    assert "等待可信签名与受限导入校验完成" not in source
    assert 'options.headers[SETUP_SESSION_HEADER] = setupSessionToken' in source
    assert "暂停下载" in source
    assert "无需 GPU" in source
    assert "开始使用" in source
    assert "先导入记忆包，再连接回信服务" in source
    assert "已有配置会自动沿用" in source
    assert "剩余" in source
    assert "安装后占用" in source
    assert "实际来源" in source
    assert 'field("许可证"' not in source
    assert "精确位置" not in source
    assert 'installation_root: "程序目录"' not in source
    assert 'local_data_root: "本地数据目录"' not in source
    assert "|| isSettingsRoute()" not in source
    assert "innerHTML" not in source


def test_memory_capability_offers_direct_offline_zip_import() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    memory_panel = source.split(
        "const renderMem0CapabilityPanel = async (panel, initialMode = false) => {", 1
    )[1].split("const videoCapabilityViewState", 1)[0]

    assert 'button("导入记忆离线包（ZIP）"' in memory_panel
    assert '{ action: "import_offline" }' in memory_panel
    assert "无需解压" in memory_panel


def test_memory_offline_import_progress_is_not_described_as_a_download() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    memory_panel = source.split(
        "const renderMem0CapabilityPanel = async (panel, initialMode = false) => {", 1
    )[1].split("const videoCapabilityViewState", 1)[0]

    assert (
        'const offlineImport = ["offline", "offline-package"].includes(payload.source);'
        in memory_panel
    )
    assert (
        'downloading: offlineImport ? "正在校验并导入离线包" : "下载中"'
        in memory_panel
    )
    assert 'queued: offlineImport ? "等待导入" : "等待下载"' in memory_panel
    assert 'field(offlineImport ? "离线包内容" : "下载量"' in memory_panel
    assert 'field(offlineImport ? "待处理" : "剩余"' in memory_panel
    assert 'button(offlineImport ? "暂停导入" : "暂停下载"' in memory_panel


def test_memory_runtime_preparation_shows_live_elapsed_time() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    memory_panel = source.split(
        "const renderMem0CapabilityPanel = async (panel, initialMode = false) => {", 1
    )[1].split("const videoCapabilityViewState", 1)[0]

    assert "let mem0RuntimeProgressStartedAt = null;" in source
    assert "const runtimeElapsedSeconds =" in memory_panel
    assert "mem0RuntimeProgressStartedAt = Date.now();" in memory_panel


def test_video_reply_setting_hydrate_waits_for_the_real_dependency_probe() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    setting = source.split("const mountVideoReplySetting = (section) => {", 1)[1].split(
        "const mountOfficialLetterImport", 1
    )[0]

    assert (
        "path === VIDEO_CAPABILITY_PATH || path === VIDEO_REPLY_SETTINGS_PATH"
        in source
    )
    assert "? 300000" in source
    assert ": 5000;" in source
    assert "正在读取设置" in setting
    assert 'await routeRequest("/toy/settings/reply-routes")' in setting
    assert "selected=result.tier" in setting


def test_video_reply_setting_mutation_waits_for_probe_and_uses_committed_value() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    mutation = source.split("const requestMutation = async (path, body) => {", 1)[
        1
    ].split("const requestSetup = async", 1)[0]
    setting = source.split("const mountVideoReplySetting = (section) => {", 1)[1].split(
        "const mountOfficialLetterImport", 1
    )[0]

    assert "path === VIDEO_REPLY_SETTINGS_PATH" in mutation
    assert "? 300000" in mutation
    assert ": 8000;" in mutation
    assert "tier:selected" in setting


def test_reply_route_settings_show_individual_readiness_without_claiming_private_repair() -> None:
    source = BOOTSTRAP_JAVASCRIPT
    setting = source.split("const mountVideoReplySetting = (section) => {", 1)[1].split(
        "const mountOfficialLetterImport", 1
    )[0]

    assert '文字＋声音＋视频' in setting
    assert '不代表每封信都会使用' in setting
    assert "离线组件" not in setting  # replies are generated in the cloud; no local components
    assert "随 Olivia 安装包提供" not in source


def test_initial_setup_dialog_survives_mailbox_route_cleanup() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for JavaScript behavior validation")
    harness = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(0, "utf8");
source = source.replace(/\}\)\(\);\s*$/, `
  globalThis.mountSettingsShell = mountShell;
})();\n`);

let dialogRemoved = false;
const dialog = { remove: () => { dialogRemoved = true; } };
const document = {
  currentScript: { dataset: { apiBase: "http://127.0.0.1:8899/" } },
  documentElement: {},
  querySelector: (selector) => selector.includes("settings-dialog") ? dialog : null,
  querySelectorAll: () => [],
};
const context = {
  URL, AbortController, document,
  MutationObserver: class { observe() {} },
  window: {
    location: { pathname: "/collection", hash: "" },
    requestAnimationFrame: () => {},
    addEventListener: () => {},
    setInterval: () => 1,
  },
};
vm.runInNewContext(source, context);
context.mountSettingsShell();
process.stdout.write(JSON.stringify({ dialogRemoved }));
'''
    completed = subprocess.run(
        [node, "-e", harness],
        input=BOOTSTRAP_JAVASCRIPT.encode("utf-8"),
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
    assert completed.returncode == 0, output
    assert json.loads(completed.stdout)["dialogRemoved"] is False


def test_statement_states_the_published_minimum_charge():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    source = 'const drawUnifiedStatement =' + BOOTSTRAP_JAVASCRIPT.split('const drawUnifiedStatement =', 1)[1].split('\n  };\n', 1)[0] + '\n  };'
    harness = r'''
const assert = require('node:assert/strict');
class Element { constructor(){this.children=[];this.style={};} append(...c){this.children.push(...c);} replaceChildren(...c){this.children=c;} }
const document={createElement:()=>new Element()};
const text=(tag,value)=>({textContent:value});
''' + source + r'''
const render=account=>{const target=new Element();drawUnifiedStatement(target,account);return target.children.map(c=>c.textContent||'').join('\n');};
const base={remaining_yuan:'1',reserved_yuan:'0',used_yuan:'0',items:[]};
const withMinimum=render({...base,minimum_charge_yuan:'0.01'});
assert.match(withMinimum,/每次最低 ¥0\.01/);
assert.match(withMinimum,/未产生费用的调用不收费/);
assert.doesNotMatch(render(base),/每次最低/);
'''
    result = subprocess.run([node, '-e', harness], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr


def test_statement_rows_show_when_each_charge_happened():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    source = 'const drawUnifiedStatement =' + BOOTSTRAP_JAVASCRIPT.split('const drawUnifiedStatement =', 1)[1].split('\n  };\n', 1)[0] + '\n  };'
    harness = r'''
const assert = require('node:assert/strict');
class Element { constructor(){this.children=[];this.style={};} append(...c){this.children.push(...c);} replaceChildren(...c){this.children=c;} }
const document={createElement:()=>new Element()};
const text=(tag,value)=>({textContent:value});
const flat=node=>node.textContent!==undefined?[node.textContent]:node.children.flatMap(flat);
''' + source + r'''
const target=new Element();
drawUnifiedStatement(target,{remaining_yuan:'1',reserved_yuan:'0',used_yuan:'0',items:[
  {label:'音乐视频',status:'settled',created_at:'2026-10-01T13:03:27+00:00',charged_yuan:'3.88',reserved_yuan:'0'},
  {label:'写回信',status:'settled',created_at:'not-a-date',charged_yuan:'0.01',reserved_yuan:'0'}]});
const lines=flat(target);
assert.ok(lines.some(line=>/2026.*10.*01/.test(line)), lines.join('|'));  // local date and time of the charge
assert.ok(lines.includes('写回信 · 已结算'));                              // an unreadable time is simply omitted
'''
    result = subprocess.run([node, '-e', harness], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
