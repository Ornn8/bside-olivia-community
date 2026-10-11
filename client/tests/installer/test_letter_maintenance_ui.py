import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_maintenance_preview_confirm_cancel_apply_restore_and_stale_error():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    source = 'const mountLetterMaintenance =' + BOOTSTRAP_JAVASCRIPT.split(
        'const mountLetterMaintenance =', 1)[1].split('const mountLocalLetterImport =', 1)[0]
    harness = r'''
const assert = require('node:assert/strict'), all=[];
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.events={};this.value='';this.style={};all.push(this)}
 append(...nodes){this.children.push(...nodes)}
 replaceChildren(...nodes){this.children=nodes}
 setAttribute(){} addEventListener(k,v){this.events[k]=v}
 click(){}
}
const document={createElement:tag=>new Element(tag)};
const text=(tag,value)=>Object.assign(new Element(tag),{textContent:value});
const button=(label,callback)=>Object.assign(text('button',label),{click:callback});
const actions=()=>new Element('div'),setButtonsBusy=(buttons,busy)=>buttons.forEach(b=>b.disabled=busy);
let confirmed=false, applied=false, stale=false, requests=[];
const confirmAction=async()=>confirmed;
const requestMutation=async(path,body)=>{
 requests.push({path,body});
 if(path.endsWith('/apply')){
  if(stale)throw {code:'LETTER_MAINTENANCE_STALE'};
  assert.equal(body.token,'snapshot');assert.deepEqual(body.selected,['hide']);applied=true;
  return {status:'APPLIED',changed:1};
 }
 return {token:'snapshot',items:[{kind:applied?'restore':'failed',left:{content:'<img onerror=evil()>',reply_text:'',created_at:null},right:null,
   options:[{id:applied?'restore':'hide',label:applied?'恢复':'收起'}]}],total:1,counts:{failed:1}};
};
'''
    harness += source + r'''
(async()=>{
 const section=new Element('section');mountLetterMaintenance(section);
 const find=label=>all.find(el=>el.tag==='button'&&el.textContent===label);
 const scan=find('检查当前信箱'),apply=find('应用所选（0）');
 assert.equal(apply.disabled,true);
 await scan.click();assert.equal(requests.length,1);assert.equal(apply.disabled,true);
 const select=all.filter(el=>el.tag==='select').at(-1);
 assert.equal(select.value,'');select.value='hide';select.events.change();assert.equal(apply.disabled,false);
 await apply.click();assert.equal(requests.length,1,'cancel must not write');
 confirmed=true;await apply.click();assert.equal(applied,true);
 assert.ok(all.some(el=>el.textContent==='已整理 · 可恢复'));
 assert.ok(all.some(el=>el.textContent?.includes('<img onerror=evil()>')));
 applied=false;await scan.click();
 const latest=all.filter(el=>el.tag==='select').at(-1);latest.value='hide';latest.events.change();
 stale=true;await apply.click();
 assert.equal(apply.disabled,true);assert.equal(scan.disabled,false);
 assert.ok(all.some(el=>el.textContent==='信箱已变化，请重新检查后选择。'));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', harness], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf8', errors='replace')


def test_real_mutation_helper_accepts_maintenance_envelopes_and_errors():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    source = 'const requestMutation =' + BOOTSTRAP_JAVASCRIPT.split(
        'const requestMutation =', 1)[1].split('const requestSetup =', 1)[0]
    harness = r'''
const assert=require('node:assert/strict');
const apiBase='http://127.0.0.1:12345';
const VIDEO_REPLY_SETTINGS_PATH='/video',LOCAL_LETTER_IMPORT_PATH='/import',MEMORY_RETRY_PATH='/memory',MEMORY_CLEAR_PATH='/memory/clear';
const DAILY_LIFE_PATH='/toy/companion/private-world/life';
const CONFIRM_HEADER='X-Olivia-Companion-Action',CONFIRM_VALUE='confirmed';
let timeoutMs=0,fail=false;
const window={setTimeout(callback,ms){timeoutMs=ms;return 1},clearTimeout(){}};
const fetch=async(url,options)=>{
 assert.equal(options.headers[CONFIRM_HEADER],'confirmed');
 return {ok:!fail,json:async()=>({code:fail?409:0,data:fail?{status:'FAILED',error_code:'LETTER_MAINTENANCE_STALE'}:{status:'READY',items:[]}})};
};
'''
    harness += source + r'''
(async()=>{
 const data=await requestMutation('/toy/letter/maintenance/preview',{});
 assert.equal(data.status,'READY');assert.equal(timeoutMs,300000);
 fail=true;
 await assert.rejects(requestMutation('/toy/letter/maintenance/apply',{}),error=>error.code==='LETTER_MAINTENANCE_STALE'&&error.dailyLifeStage===undefined);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', harness], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf8', errors='replace')
