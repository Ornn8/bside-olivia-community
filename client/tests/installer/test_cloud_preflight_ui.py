import subprocess
from pathlib import Path

import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT


def test_cloud_dialog_retry_cancel_and_chat_button_contrast(tmp_path):
    browser = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not browser.is_file():
        pytest.skip('Chromium unavailable')
    source = BOOTSTRAP_JAVASCRIPT
    dialog = source[source.index('  const confirmReplyRoute ='):source.index('  const composerCovers =')]
    prepare = source[source.index('  window.__oliviaPrepareLetterRoute ='):source.index('  const mountProactiveSetting =')]
    css = '\n'.join(line for line in PERSONAL_CHAT_SETUP_JAVASCRIPT.splitlines() if '[data-olivia-personal-chat-setup] .olivia-chat-action' in line)
    page = tmp_path / 'preflight.html'
    page.write_text('''<!doctype html><meta charset="utf-8"><style>body{background:#181818;color:black}'''+css+'''</style>
<div data-olivia-personal-chat-setup><button class="olivia-chat-action">重新绑定</button><button class="olivia-chat-action">一键安装</button></div><pre id="result"></pre>
<script>
const text=(tag,value)=>{const el=document.createElement(tag);el.textContent=value;return el};
const actions=()=>document.createElement('div');
const button=(label,fn)=>{const el=text('button',label);el.onclick=fn;return el};
const REPLY_ROUTE_LABELS={singing_video:'唱歌'};
const check=(ok,msg)=>{if(!ok)throw Error(msg)};
'''+dialog+prepare+'''
const apiBase='http://127.0.0.1:8899',proactiveState={busy:false},coverComposer=null;
let checks=0;
const routeRequest=async()=>{checks++;return {requested_route:'singing_video',ready:checks>1,
 readiness:{backend:'remote',error_code:'GPU_CONNECTION_TIMEOUT'},token:'recovered',reply_mode:'singing_video'}};
const requestSetup=async()=>({paid:false});
(async()=>{try{
 for(const b of document.querySelectorAll('.olivia-chat-action')){
   const style=getComputedStyle(b);check(style.color==='rgb(241, 238, 232)','dark inherited text');
   check(style.backgroundColor==='rgb(41, 42, 45)','missing dark background');
 }
 let pending=confirmReplyRoute('singing_video',false,false,{backend:'remote',error_code:'GPU_CONNECTION_TIMEOUT'});
 let modal=document.querySelector('dialog');
 check(modal.textContent.includes('云端检查超时'),'missing cause');
 check(!modal.textContent.includes('本地组件'),'wrong backend');
 [...modal.querySelectorAll('button')].find(b=>b.textContent==='重新检查').click();
 check(await pending==='retry','retry accepted send');check(!document.querySelector('dialog'),'leaked dialog');
 pending=confirmReplyRoute('singing_video',false,false,{backend:'remote',error_code:'GPU_AUTH_FAILED'});
 document.querySelector('dialog button').click();check(await pending===false,'cancel accepted');
 pending=confirmReplyRoute('singing_video',false,false,{backend:'local'});
 check(document.querySelector('dialog').textContent.includes('本地组件'),'local hint lost');
 document.querySelector('dialog button').click();await pending;
 const config={url:'/toy/letter/send',data:{content:'synthetic'}};
 const prepared=window.__oliviaPrepareLetterRoute(config);
 await new Promise(r=>setTimeout(r,0));
 check(checks===1,'automatic retry');
 [...document.querySelectorAll('dialog button')].find(b=>b.textContent==='重新检查').click();
 const ready=await prepared;
 check(checks===2,'retry did not recheck');
 check(ready.data.material.route_preview_token==='recovered','recovered token missing');
 check(!ready.data.material.route_allow_once,'retry granted consent');
 document.getElementById('result').textContent='PASS';
}catch(e){document.getElementById('result').textContent='FAIL:'+e.message}})();
</script>''', encoding='utf-8')
    result = subprocess.run([str(browser), '--headless', '--disable-gpu', '--no-first-run',
        '--disable-background-networking', f'--user-data-dir={tmp_path / "profile"}',
        '--virtual-time-budget=1500', '--dump-dom', page.as_uri()], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=30)
    assert '<pre id="result">PASS</pre>' in result.stdout, result.stdout[-2500:]
