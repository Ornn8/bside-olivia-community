import shutil
import subprocess
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_gpu_form_tests_candidate_and_clears_saved_key():
    node=shutil.which('node')
    if not node: pytest.skip('Node unavailable')
    source='const mountGPUSettings ='+BOOTSTRAP_JAVASCRIPT.split('const mountGPUSettings =',1)[1].split('const mountCloudService =',1)[0]
    harness=r'''
const assert=require('node:assert/strict');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.style={};this.value='';this.events={};}
  append(...v){this.children.push(...v);}
  setAttribute(){}
  addEventListener(k,v){this.events[k]=v;}
  querySelectorAll(tag){return this.children.flatMap(c=>[...(c.tag===tag?[c]:[]),...c.querySelectorAll(tag)]);}
}
const document={createElement:t=>new Element(t)};
const text=(t,v)=>Object.assign(new Element(t),{textContent:v});
const button=(label,fn)=>Object.assign(text('button',label),{click:fn});
const actions=()=>new Element('div');
const setButtonsBusy=(buttons,b)=>buttons.forEach(x=>x.disabled=b);
let setupSessionToken='session',refreshes=0;
const refreshVideoReplySetting=async()=>{refreshes++;};
const SETUP_STATUS_PATH='unused',requests=[];
const requestSetup=async(path,body)=>{
  requests.push(body);
  if(body.action==='settings_test')return {kinds:['tts']};
  if(body.action==='settings_save')return {route:body.route,url:body.url,has_key:true};
  return {route:'local',url:'',has_key:false};
};
'''+source+r'''
(async()=>{
 const root=new Element('root');mountGPUSettings(root);
 await new Promise(r=>setImmediate(r));
 const mode=root.querySelectorAll('select')[0],inputs=root.querySelectorAll('input');
 mode.value='remote';inputs[0].value='https://new.example';inputs[1].value='test-key';
 const buttons=root.querySelectorAll('button');
 await buttons[0].click();
 assert.equal(requests.at(-1).action,'settings_test');
 assert.equal(inputs[1].value,'test-key');
 await buttons[1].click();
 assert.deepEqual(requests.at(-1),{action:'settings_save',route:'remote',url:'https://new.example',key:'test-key'});
 assert.equal(inputs[1].value,'');assert.equal(refreshes,1);
 await buttons[2].click();assert.equal(mode.value,'local');assert.equal(inputs[0].value,'');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',harness],capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
