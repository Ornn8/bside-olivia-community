import shutil
import subprocess
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_resend_rejection_explains_existing_attempt_without_confirming_or_mutating(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    source = BOOTSTRAP_JAVASCRIPT
    start = source.index('  window.__oliviaPrepareLetterRoute =')
    end = source.index('  const mountProactiveSetting', start)
    script = r'''
const assert=require('node:assert/strict');
global.window={};const apiBase='http://127.0.0.1:8899';
const proactiveState={busy:false},coverComposer=null;
let code;
async function routeRequest(){throw new Error(code)}
async function confirmAction(){assert.fail('Rejected resend must not confirm a new charge')}
''' + source[start:end] + r'''
(async()=>{
 for(const [errorCode, message] of [
   ['LETTER_IN_PROGRESS',/正在生成回信/],
   ['LETTER_SUPERSEDED',/已经重新寄出/],
   ['LETTER_RESEND_NOT_ALLOWED',/当前不能重寄/]]){
   code=errorCode;
   const request={url:'/toy/letter/resend',data:{letterId:'original'}};
   await assert.rejects(()=>window.__oliviaPrepareLetterRoute(request),error=>{
     assert.match(error.message,message);
     assert.match(error.message,/本次没有重复寄出/);
     assert.doesNotMatch(error.message,/信件尚未寄出/);
     assert.equal(error.config,request);
     return true;
   });
   assert.deepEqual(request.data,{letterId:'original'});
 }
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path = tmp_path / 'resend-errors.cjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_resend_loads_original_preview_and_requires_image_confirmation(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    source = BOOTSTRAP_JAVASCRIPT
    start = source.index('  window.__oliviaPrepareLetterRoute =')
    end = source.index('  const mountProactiveSetting', start)
    script = r'''
const assert=require('node:assert/strict');
global.window={};const apiBase='http://127.0.0.1:8899';
const proactiveState={busy:false},coverComposer=null;
let accepted=false, prompts=[],previews=[];
async function routeRequest(path,body){
  previews.push({path,body});
  return {token:'verified-original-token',image_enabled:true,image_requested:true,reply_mode:'text_letter'};
}
async function confirmAction(message){prompts.push(message);return accepted}
''' + source[start:end] + r'''
(async()=>{
 const request={url:'/toy/letter/resend',data:{letterId:'failed-original'}};
 await assert.rejects(()=>window.__oliviaPrepareLetterRoute(request),e=>e.code==='ERR_CANCELED');
 assert.equal(prompts.length,1);assert.match(prompts[0],/图片/);
 assert.deepEqual(previews[0].body,{letter_id:'failed-original'});
 assert.equal(request.data.material,undefined);
 accepted=true;
 const result=await window.__oliviaPrepareLetterRoute(request);
 assert.equal(result.data.material.route_preview_token,'verified-original-token');
 assert.equal(result.data.letterId,'failed-original');
 assert.equal(prompts.length,2);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    path=tmp_path/'resend.cjs'
    path.write_text(script,encoding='utf-8')
    result=subprocess.run([node,str(path)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
