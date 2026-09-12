"""Exercise the shipped panel's click handler, including cancellation and fallback."""
import json
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


@pytest.mark.parametrize("outcome", ["success", "cancel", "offline"])
def test_one_click_update(outcome):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the client JavaScript")
    source = BOOTSTRAP_JAVASCRIPT.split("const renderLocalUpdatePanel = ", 1)[1].split(
        "const loadDialogData = ", 1
    )[0].strip().removesuffix(";")
    script = r'''
const assert = require('node:assert/strict');
const {source, outcome} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const buttons = [], calls = [];
function element(value = '') {
  return {textContent: value, children: [], disabled: false,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; }, setAttribute() {}};
}
const document = {createElement: () => element()};
const text = (tag, value) => element(value);
const setupInput = () => ({wrapper: element(), input: element()});
const button = (label, click) => { const b = element(label); b.click = click; buttons.push(b); return b; };
const actions = () => element();
const setButtonsBusy = (list, busy) => list.forEach(b => b.disabled = busy);
const confirmAction = () => { throw Error('unexpected extra confirmation'); };
const requestUpdate = async payload => {
  calls.push(payload);
  assert(buttons.every(b => b.disabled));
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
  assert.deepEqual(calls.map(c => c.action), outcome === 'cancel' ? ['select'] : ['select','apply_verified']);
  assert(buttons.every(b => !b.disabled));
  const result = panel.children.at(-1).textContent;
  assert(result.includes(outcome === 'success' ? '1.2.3 已安装' : outcome === 'cancel' ? '已取消' : '尚未安装'));
  if (outcome === 'offline') assert(panel.children.some(c => c.textContent.includes('patch.oliviapatch')));
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], input=json.dumps({"source": source, "outcome": outcome}),
                            text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
