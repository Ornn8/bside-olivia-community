import shutil
import subprocess
import pytest
import asyncio
import json
from runtime.gpu_settings import GPUSettings
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_gpu_form_tests_candidate_and_clears_saved_key(tmp_path, monkeypatch):
    from runtime.remote_generation import RemoteGeneration
    async def capabilities(self, action, data):
        return {'kinds':['tts'], 'shared_assets':[]}
    monkeypatch.setattr(RemoteGeneration, 'request', capabilities)
    service = GPUSettings(tmp_path, environment={})
    responses = {'settings_status': service.status(),
                 'settings_use_olivia': dict(service.status(), route='remote', url='https://new.example', has_key=True),
                 'settings_test': asyncio.run(service.test('https://new.example', 'test-key')),
                 'settings_clear': service.status()}
    responses.update(billing_prices={'status': 'OK', 'pricing': None}, billing_statement={
        'status': 'OK', 'currency': 'CNY', 'remaining_yuan': '99.85', 'used_yuan': '0.15', 'reserved_yuan': '0.00',
        'items': [{'source':'gpu', 'label':'语音生成', 'status':'settled', 'charged_yuan':'0.15',
                   'reserved_yuan':'0.00', 'released_yuan':'0.00'},
                  {'source':'relay', 'label':'图片识别', 'status':'settled', 'charged_yuan':'0.00001234',
                   'reserved_yuan':'0.00', 'released_yuan':'0.00'}]})
    node=shutil.which('node')
    if not node: pytest.skip('Node unavailable')
    source=('const drawUnifiedStatement ='+BOOTSTRAP_JAVASCRIPT.split('const drawUnifiedStatement =',1)[1].split('const mountRelayAccount =',1)[0]
        +'const mountGPUSettings ='+BOOTSTRAP_JAVASCRIPT.split('const mountGPUSettings =',1)[1].split('const mountCloudService =',1)[0])
    harness=r'''
const assert=require('node:assert/strict');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.style={};this.value='';this.events={};}
  append(...v){this.children.push(...v);}
  replaceChildren(...v){this.children=[...v];}
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
const apiBase='http://127.0.0.1:8899',window={setTimeout,clearTimeout};
const SETUP_CONFIRM_HEADER='X-Confirm',CONFIRM_VALUE='confirmed',SETUP_SESSION_HEADER='X-Session';
const fetch=async(path,options)=>{
  const body=JSON.parse(options.body);
  requests.push(body);
  const payload=body.action==='settings_save' ? {...responses.settings_status,route:body.route,url:body.url,has_key:true} : responses[body.action];
  return {ok:true,json:async()=>payload};
};
'''+ 'const responses='+json.dumps(responses)+';\n'+ 'const requestSetup ='+BOOTSTRAP_JAVASCRIPT.split('const requestSetup =',1)[1].split('const requestCapability =',1)[0]+source+r'''
(async()=>{
 const root=new Element('root');mountGPUSettings(root);
 await new Promise(r=>setImmediate(r));
 const mode=root.querySelectorAll('select')[0],inputs=root.querySelectorAll('input');
 mode.value='remote';inputs[0].value='https://new.example';inputs[1].value='test-key';
 const buttons=root.querySelectorAll('button');
 const click=label=>buttons.find(b=>b.textContent===label).click();
 await click('测试连接');
 assert.ok(root.querySelectorAll('p').some(p=>p.textContent.startsWith('连接成功')));
 assert.equal(requests.at(-1).action,'settings_test');
 assert.equal(inputs[1].value,'test-key');
 await click('保存生成设置');
 assert.deepEqual(requests.at(-1),{action:'settings_save',route:'remote',url:'https://new.example',key:'test-key'});
 assert.equal(inputs[1].value,'');assert.equal(refreshes,1);
 await click('清除连接');assert.equal(mode.value,'local');assert.equal(inputs[0].value,'');
 inputs[0].value='https://new.example';
 assert.ok(!buttons.some(b=>b.textContent==='领取测试 Key'));
 await click('使用我的 Olivia Key');
 assert.deepEqual(requests.at(-1),{action:'settings_use_olivia'});
 assert.equal(inputs[1].value,'');assert.equal(mode.value,'remote');
 assert.ok(root.querySelectorAll('p').some(p=>p.textContent.startsWith('已启用云端生成')));
 await click('刷新账单');
 assert.equal(requests.at(-1).action,'billing_statement');
 assert.ok(root.querySelectorAll('p').some(p=>p.textContent.includes('¥99.85')));
 assert.ok(root.querySelectorAll('span').some(p=>p.textContent==='语音生成 · 已结算'));
 assert.ok(root.querySelectorAll('span').some(p=>p.textContent==='−¥0.15'));
 assert.ok(root.querySelectorAll('span').some(p=>p.textContent==='−¥0.00001234'));
 const visible=root.querySelectorAll('p').map(p=>p.textContent).join(' ');
 for(const internal of ['synthetic-task','test-v1','0.75','倍率','CPU','local-gpu-active'])assert.ok(!visible.includes(internal));
 responses.billing_statement=null;
 await click('刷新账单');
 assert.ok(root.querySelectorAll('p').some(p=>p.textContent.includes('余额暂时无法读取')));
 assert.ok(!root.querySelectorAll('p').some(p=>p.textContent.includes('¥99.85')));
 inputs[0].value='https://unsaved.example';const count=requests.length;
 await click('刷新账单');assert.equal(requests.length,count);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',harness],capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
