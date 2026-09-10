import pytest
import shutil
import subprocess

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT

from patch_companion_settings import _repair_native_proactive_collection, CompanionSettingsPatchError


def test_proactive_selection_skips_paired_letter_and_updates_native_audio_props():
    # Synthetic fragments of the supported adapter contract; no original bundle.
    model = 'return{id:e.letterId,isUnread:e.isRead===0,coverId:e.coverId||"",'
    source = model + model + (
        'h=j({get:()=>i("mailbox_welcome_content"),set:()=>{}})'
        ':x.selectedMail&&!o(p)?paired:o(p)&&o(l).welcomeMailRead?welcome:empty;'
        'k(ks,{modelValue:o(h),"onUpdate:modelValue":I[1]||(I[1]=A=>be(h)?h.value=A:null),'
        'class:"w-[516px] aspect-[16/9]","is-visible":!0,readonly:"",'
        'timestamp:((M=(E=x.selectedMail)==null?void 0:E.received)==null?void 0:M.timestamp)??0,'
        'type:"text"},null,8,["modelValue","timestamp"])'
    )
    patched = _repair_native_proactive_collection(source)
    assert 'x.selectedMail&&!o(p)&&x.selectedMail.origin!=="proactive"?' in patched
    assert 'x.selectedMail.origin==="proactive")?welcome' in patched
    assert 'a.selectedMail.received' in patched
    assert '["modelValue","timestamp","coverId","audioUrl","audioStatus","songUrl"]' in patched
    assert _repair_native_proactive_collection(patched) == patched


def test_changed_supported_native_shape_fails_before_partial_patch():
    source = 'return{id:e.letterId,isUnread:e.isRead===0,coverId:e.coverId||"",' * 2
    with pytest.raises(CompanionSettingsPatchError, match='COMPANION_PROACTIVE_ANCHOR_INVALID'):
        _repair_native_proactive_collection(source)


def test_busy_send_gate_keeps_existing_mailbox_readable(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js unavailable')
    begin = BOOTSTRAP_JAVASCRIPT.index('window.__oliviaPrepareLetterRoute = async (config) => {')
    end = BOOTSTRAP_JAVASCRIPT.index('    const body =', begin)
    gate = BOOTSTRAP_JAVASCRIPT[begin:end] + 'return config; };'
    script = tmp_path / 'gate.cjs'
    script.write_text('const window={}; const apiBase="http://127.0.0.1:4000"; const proactiveState={busy:true};\n'
                      + gate + '''
(async()=>{
  const detail={url:'/toy/letter/detail'};
  if(await window.__oliviaPrepareLetterRoute(detail)!==detail) throw Error('read blocked');
  try { await window.__oliviaPrepareLetterRoute({url:'/toy/letter/send'}); }
  catch(error) { if(error.code==='PROACTIVE_LETTER_BUSY') return; throw error; }
  throw Error('send was allowed');
})();
''', encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_native_save_supplies_existing_explicit_action_header(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js unavailable')
    begin = BOOTSTRAP_JAVASCRIPT.index('  const saveProactiveSettings = async')
    end = BOOTSTRAP_JAVASCRIPT.index('  const requestDiagnosticExport', begin)
    script = tmp_path / 'save.cjs'
    script.write_text('''
const apiBase='http://127.0.0.1:4000', PROACTIVE_SETTINGS_PATH='/toy/proactive/settings';
const CONFIRM_HEADER='X-Olivia-Companion-Action', CONFIRM_VALUE='confirmed';
const publishProactiveState=()=>{};
global.fetch=async(url, options)=>{
  if(options.headers[CONFIRM_HEADER]!==CONFIRM_VALUE) throw Error('confirmation missing');
  return {ok:true, json:async()=>({code:0,data:JSON.parse(options.body)})};
};
''' + BOOTSTRAP_JAVASCRIPT[begin:end] + '\nsaveProactiveSettings({enabled:true,allow_voice:true,login_check_enabled:false});', encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
