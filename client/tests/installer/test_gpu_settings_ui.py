import shutil
import subprocess
import pytest
import json
from runtime.gpu_settings import GPUSettings
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_account_entry_shows_balance_and_unified_statement(tmp_path):
    service = GPUSettings(tmp_path, environment={}, account_key=lambda: 'olivia-synthetic-account')
    responses = {'settings_status': service.status()}
    responses.update(billing_prices={'status': 'OK', 'pricing': None}, billing_statement={
        'status': 'OK', 'currency': 'CNY', 'remaining_yuan': '99.85', 'used_yuan': '0.15', 'reserved_yuan': '0.00',
        'items': [{'source':'gpu', 'label':'语音生成', 'status':'settled', 'charged_yuan':'0.15',
                   'reserved_yuan':'0.00', 'released_yuan':'0.00'},
                  {'source':'relay', 'label':'图片识别', 'status':'settled', 'charged_yuan':'0.00001234',
                   'reserved_yuan':'0.00', 'released_yuan':'0.00'}]})
    node=shutil.which('node')
    if not node: pytest.skip('Node unavailable')
    source=('const drawUnifiedStatement ='+BOOTSTRAP_JAVASCRIPT.split('const drawUnifiedStatement =',1)[1].split('const mountRelayAccount =',1)[0]
        +'const refreshAccountEntry ='+BOOTSTRAP_JAVASCRIPT.split('const refreshAccountEntry =',1)[1].split('let scheduled = false;',1)[0])
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
const entry=Object.assign(new Element('button'),{textContent:'账户'});
const document={createElement:t=>new Element(t),querySelector:()=>entry};
const text=(t,v)=>Object.assign(new Element(t),{textContent:v});
const button=(label,fn)=>Object.assign(text('button',label),{click:fn});
const actions=()=>new Element('div');
const setButtonsBusy=(buttons,b)=>buttons.forEach(x=>x.disabled=b);
let setupSessionToken='session';
const SETUP_STATUS_PATH='unused',requests=[];
const apiBase='http://127.0.0.1:8899',window={setTimeout,clearTimeout};
const SETUP_CONFIRM_HEADER='X-Confirm',CONFIRM_VALUE='confirmed',SETUP_SESSION_HEADER='X-Session';
const fetch=async(path,options)=>{
  const body=JSON.parse(options.body);
  requests.push(body);
  return {ok:true,json:async()=>responses[body.action]};
};
'''+ 'const responses='+json.dumps(responses)+';\n'+ 'const requestSetup ='+BOOTSTRAP_JAVASCRIPT.split('const requestSetup =',1)[1].split('const requestCapability =',1)[0]+source+r'''
(async()=>{
 await refreshAccountEntry();
 assert.deepEqual(requests.map(r=>r.action),['settings_status','billing_statement']);
 assert.equal(entry.textContent,'账户 ¥99.85');
 const root=new Element('root');
 drawUnifiedStatement(root,responses.billing_statement);
 // The remaining amount lives in the summary card now; the records list only lists records.
 assert.ok(!root.querySelectorAll('p').some(p=>p.textContent.includes('¥99.85')));
 assert.ok(root.querySelectorAll('span').some(p=>p.textContent==='语音生成'));
 assert.ok(root.querySelectorAll('span').some(p=>p.textContent==='已结算'));
 assert.equal(root.querySelectorAll('button').find(b=>b.textContent==='显示更早的记录').hidden,true);
 assert.ok(root.querySelectorAll('span').some(p=>p.textContent==='−¥0.15'));
 assert.ok(root.querySelectorAll('span').some(p=>p.textContent==='−¥0.00001234'));
 const visible=root.querySelectorAll('p').map(p=>p.textContent).join(' ');
 for(const internal of ['synthetic-task','test-v1','0.75','倍率','CPU','local-gpu-active'])assert.ok(!visible.includes(internal));
 responses.billing_statement=null;
 await refreshAccountEntry();
 assert.equal(entry.textContent,'账户');
 responses.settings_status={...responses.settings_status,has_key:false};
 await refreshAccountEntry();
 assert.equal(entry.textContent,'账户');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',harness],capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
