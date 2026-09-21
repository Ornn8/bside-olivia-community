import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_relay_error_notice_is_actionable_dismissible_and_does_not_echo_server_text(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for settings interaction test')
    source = BOOTSTRAP_JAVASCRIPT.split('  const loader =', 1)[0] + '})();'
    script = r'''
const assert = require('node:assert/strict');
const window = {}, boxes=[];
const document = {
  getElementById: id=>boxes.find(x=>x.id===id),
  createElement: tag=>({tag,style:{},children:[],setAttribute(){},
    addEventListener(event,fn){this.click=fn},append(...items){this.children.push(...items)},
    remove(){boxes.splice(boxes.indexOf(this),1)}}),
  body:{append:box=>boxes.push(box)}
};
''' + source + r'''
window.__oliviaExplainRelayFailure({data:{error_code:'LLM_USAGE_PENDING',message:'private body'}});
assert.equal(boxes.length,1);
assert.match(boxes[0].children[0].textContent,/等待核对/);
assert.ok(!JSON.stringify(boxes).includes('private body'));
window.__oliviaExplainRelayFailure({error_code:'LLM_USAGE_PENDING'});
assert.equal(boxes.length,1);
boxes[0].children[1].click();
assert.equal(boxes.length,0);
window.__oliviaExplainRelayFailure({error_code:'UNKNOWN',message:'private body'});
assert.equal(boxes.length,0);
window.__oliviaExplainRelayFailure({error_code:'LLM_QUOTA_EXHAUSTED'});
assert.match(boxes[0].children[0].textContent,/余额/);
'''
    path = tmp_path / 'relay-notice.cjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
