import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from runtime.personal_chat._patch_companion_settings_base import _repair_native_reply_failure


def test_failure_code_reaches_native_error_paper_and_updates_after_retry(tmp_path):
    source = '''
const map=e=>({letterStatus:e.letterStatus,});
const component={__name:"MailBoxReplyContent",props:{}};
const content=i=>F(ks,{key:i.mail.id});
const dynamic=["modelValue","videoUrl","timestamp","type"];
const changed=(re,Ee)=>re.isUnread!==Ee.isUnread;
const title=A=>v(o(i)("mailbox_reply_error_title"));
const hint=A=>v(o(i)("mailbox_reply_error_hint"));
'''
    patched = _repair_native_reply_failure(source)
    assert _repair_native_reply_failure(patched) == patched
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for native component projection')
    bootstrap = BOOTSTRAP_JAVASCRIPT.split('  const loader =', 1)[0] + '})();'
    script = '''const assert=require('node:assert/strict');
const window={},ks={},F=(component,props)=>props,v=x=>x,o=x=>x,i=key=>key;
''' + bootstrap + patched + '''
const row=map({letterStatus:3,replyErrorCode:'REPLY_REWRITE_FAILED'});
const props=content({mail:row});
assert.equal(props.errorCode,'REPLY_REWRITE_FAILED');
assert.ok(Object.hasOwn(component.props,'errorCode'));
assert.ok(dynamic.includes('errorCode'));
assert.match(title(props),/修改时遇到了问题/);
assert.ok(!title(props).includes('还没有读到'));
assert.match(hint(props),/诊断包/);
const next=map({letterStatus:3,error_code:'REPLY_QUALITY_BLOCKED'});
assert.equal(changed(next,row),true);
assert.match(title(content({mail:next})),/检查后仍有问题/);
assert.match(title({errorCode:'private draft'}),/这封回信暂时没能完成/);
assert.equal(window.__oliviaReplyFailureMessage('__proto__'),null);
'''
    path = tmp_path / 'native-failure.cjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_upgrade_with_audio_props_keeps_error_code_dynamic():
    for prefix in ('', '"coverId",', '"replyWaitReason",'):
        source = '[' + prefix + '"audioUrl","audioStatus","songUrl","modelValue","videoUrl","timestamp","type"]'
        patched = _repair_native_reply_failure(source)
        assert patched.startswith('["errorCode",')
        assert _repair_native_reply_failure(patched) == patched


@pytest.mark.parametrize('code,expected', [('REPLY_QUALITY_BLOCKED','REPLY_QUALITY_BLOCKED'),
    ('REWRITE_INPUT_TOO_LARGE','REPLY_REWRITE_FAILED'), ('REPLY_REWRITE_FAILED','REPLY_REWRITE_FAILED'),
    ('private draft',None)])
def test_failure_code_is_identical_in_list_and_detail(code, expected):
    from original_client_letter_contract import serialize_letter_summary, serialize_letter_detail
    row = {'letter_id': 'synthetic', 'letter_status': 'FAILED', 'error_code': code}
    summary = serialize_letter_summary(row, include_legacy_aliases=True)
    detail = serialize_letter_detail(row, scope='current', include_legacy_aliases=True)
    assert summary.get('replyErrorCode') == detail.get('replyErrorCode') == expected
    row['letter_status'] = 'PENDING'
    assert 'replyErrorCode' not in serialize_letter_summary(row)


@pytest.mark.parametrize('code', ['LLM_TIMEOUT', 'LLM_UNAVAILABLE'])
def test_transient_reply_failure_code_is_identical_in_list_and_detail(code):
    from original_client_letter_contract import serialize_letter_summary, serialize_letter_detail
    row = {'letter_id': 'synthetic', 'letter_status': 'FAILED', 'error_code': code}
    assert serialize_letter_summary(row)['replyErrorCode'] == code
    assert serialize_letter_detail(row, scope='current')['replyErrorCode'] == code


def test_missing_or_transient_reply_code_never_claims_the_incoming_letter_was_not_read(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for native component projection')
    source = '''const title=A=>v(o(i)("mailbox_reply_error_title"));
const hint=A=>v(o(i)("mailbox_reply_error_hint"));'''
    patched = _repair_native_reply_failure(source)
    bootstrap = BOOTSTRAP_JAVASCRIPT.split('  const loader =', 1)[0] + '})();'
    script = '''const assert=require('node:assert/strict');
const window={},v=x=>x,o=x=>x,i=key=>key==='mailbox_reply_error_title'?'寄信通道忙，她还没有读到':'等会重寄';
''' + bootstrap + patched + '''
for (const code of ['', 'PRIVATE_UNKNOWN', 'LLM_TIMEOUT', 'LLM_UNAVAILABLE']) {
  assert.ok(!title({errorCode:code}).includes('还没有读到'));
  assert.ok(!title({errorCode:code}).includes('寄信通道忙'));
}
for (const code of ['REPLY_QUALITY_BLOCKED', 'REPLY_REWRITE_FAILED', 'LLM_TIMEOUT', 'LLM_UNAVAILABLE']) {
  assert.equal(title({errorCode:code}),window.__oliviaReplyFailureMessage(code));
}
assert.equal(window.__oliviaReplyFailureMessage('REPLY_QUALITY_BLOCKED','category'),'quality');
assert.equal(window.__oliviaReplyFailureMessage('LLM_TIMEOUT','category'),'transient_reply');
'''
    path = tmp_path / 'failure-classification.cjs'
    path.write_text(script, encoding='utf-8')
    actual = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=15)
    assert actual.returncode == 0, actual.stderr
