import subprocess
from pathlib import Path

import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_pending_claim_hides_key_and_only_mounts_billing_after_success(tmp_path):
    browser=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not browser.is_file(): pytest.skip('Chromium unavailable')
    source=BOOTSTRAP_JAVASCRIPT
    ui=source[source.index('  const mountRelayAccount ='):source.index('  const mountRelayBalance =')]
    css=next(line for line in source.splitlines() if '[data-olivia-relay-dialog] [hidden]' in line)
    page=tmp_path/'claim.html'
    page.write_text('''<!doctype html><meta charset="utf-8"><style>'''+css+'''</style>
<div data-olivia-companion-settings-dialog><div data-olivia-relay-dialog><div id="panel"></div></div></div><pre id="result"></pre>
<script>
const text=(tag,value)=>{const el=document.createElement(tag);el.textContent=value;return el};
const actions=()=>document.createElement('div');
const button=(label,fn)=>{const el=text('button',label);el.onclick=fn;return el};
const setupInput=(label,type)=>{const wrapper=text('label',label),input=document.createElement('input');wrapper.style.display='flex';input.type=type;wrapper.append(input);return {wrapper,input}};
const setButtonsBusy=(buttons,value)=>buttons.forEach(b=>b.disabled=value);
let configured=false,fail=true,billingCalls=0;
const mountRelayBalance=()=>{billingCalls++};
const requestSetup=async(path,{action})=>{
 if(action==='account')return {configured,registration_pending:!configured,key_prefix:configured?'olivia-example':''};
 if(action==='claim'){if(fail)throw Object.assign(Error(),{code:'RELAY_TIMEOUT'});configured=true;return {registered:true}}
};
'''+ui+'''
const wait=()=>new Promise(r=>setTimeout(r,0));
const check=(ok,msg)=>{if(!ok)throw Error(msg)};
(async()=>{try{
 mountRelayAccount(document.getElementById('panel'));await wait();
 const label=document.querySelector('label'),claim=[...document.querySelectorAll('button')].find(b=>b.textContent==='重试注册');
 check(getComputedStyle(label).display==='none','pending key exposed');
 check(billingCalls===0,'billing before registration');
 claim.click();await wait();check(document.body.textContent.includes('RELAY_TIMEOUT'),'missing cause');
 check(!claim.disabled,'retry blocked');fail=false;claim.click();await wait();
 check(billingCalls===1,'billing not shown after success');
 check(getComputedStyle(label).display!=='none','registered key hidden');
 check(getComputedStyle(claim).display==='none','claim still shown');
 document.getElementById('result').textContent='PASS';
}catch(e){document.getElementById('result').textContent='FAIL:'+e.message}})();
</script>''',encoding='utf-8')
    result=subprocess.run([str(browser),'--headless','--disable-gpu','--no-first-run',
        '--disable-background-networking',f'--user-data-dir={tmp_path / "profile"}',
        '--virtual-time-budget=1500','--dump-dom',page.as_uri()],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    assert '<pre id="result">PASS</pre>' in result.stdout,result.stdout[-2000:]
