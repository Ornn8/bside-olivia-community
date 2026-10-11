"""Exercise the shipped panel's click handler, including cancellation and fallback."""
import json
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


@pytest.mark.parametrize("outcome", ["success", "cancel", "offline", "later", "restart_error"])
def test_one_click_update(outcome):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the client JavaScript")
    source = BOOTSTRAP_JAVASCRIPT.split("const renderLocalUpdatePanel = ", 1)[1].split(
        "const loadDialogData = ", 1
    )[0].strip().removesuffix(";")
    helper = BOOTSTRAP_JAVASCRIPT.split("const setDiagnosticDetails = ", 1)[1].split(
        "const memoryClearFailureMessage = ", 1)[0].strip().removesuffix(";")
    script = r'''
const assert = require('node:assert/strict');
const {source, helper, outcome} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const buttons = [], calls = [];
const window = {setTimeout(resolve) {
  if (outcome === 'later') buttons.find(b => b.textContent === '稍后手动重启').click();
  resolve();
}};
function element(value = '') {
  return {textContent: value, children: [], disabled: false,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; }, setAttribute() {}};
}
const document = {createElement: () => element()};
const text = (tag, value) => element(value);
const setDiagnosticDetails = eval('(' + helper + ')');
const setupInput = () => ({wrapper: element(), input: element()});
const button = (label, click) => { const b = element(label); b.click = click; buttons.push(b); return b; };
const actions = () => element();
const setButtonsBusy = (list, busy) => list.forEach(b => b.disabled = busy);
const confirmAction = () => { throw Error('unexpected extra confirmation'); };
const requestUpdate = async (payload, path) => {
  calls.push(payload);
  assert(buttons.filter(b => b.textContent !== '稍后手动重启').every(b => b.disabled));
  if (path === '/toy/update/restart') {
    if (outcome === 'restart_error') throw {code: 'UPDATE_RESTART_UNAVAILABLE'};
    return {status: 'RESTARTING'};
  }
  if (payload.action === 'select') return outcome === 'cancel'
    ? {status: 'CANCELLED'} : {status: 'SELECTED', package_path: 'F:/patch.oliviapatch'};
  assert.equal(payload.action, 'apply_verified');
  if (outcome === 'offline') throw {code: 'UPDATE_CHECKSUM_UNAVAILABLE'};
  return {status: 'APPLIED', version: '1.2.3'};
};
const render = eval('(' + source + ')');
const panel = element();
render(panel);
(async () => {
  await buttons.find(b => b.textContent === '选择补丁并更新').click();
  assert.deepEqual(calls.map(c => c.action), outcome === 'cancel' ? ['select'] :
    ['success','restart_error'].includes(outcome) ? ['select','apply_verified',undefined] : ['select','apply_verified']);
  assert(buttons.every(b => !b.disabled));
  const result = panel.children.at(-2).textContent;
  assert(result.includes(['success','later','restart_error'].includes(outcome) ? '1.2.3 已安装' : outcome === 'cancel' ? '已取消' : '尚未安装'));
  if (['later','restart_error'].includes(outcome)) assert(result.includes('关闭并重新打开 Olivia 后生效'));
  if (outcome === 'success') assert(result.includes('正在自动重启 Olivia'));
  if (outcome === 'offline') {
    assert(panel.children.some(c => c.textContent.includes('patch.oliviapatch')));
    assert.equal(panel.children.at(-2).__oliviaDiagnosticDetails.__oliviaCodeNode.textContent,'UPDATE_CHECKSUM_UNAVAILABLE');
    assert(!result.includes('UPDATE_CHECKSUM_UNAVAILABLE'));
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], input=json.dumps({"source": source, "helper": helper, "outcome": outcome}),
                            text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
