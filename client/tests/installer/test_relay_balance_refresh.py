import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_balance_refresh_visibility_failure_and_remount():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = 'const mountRelayBalance =' + BOOTSTRAP_JAVASCRIPT.split('  const mountRelayBalance =', 1)[1].split('  const renderLlmSetupPanel =', 1)[0]
    harness = r'''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const elements=[],timers=new Map();let seq=0,amount='10',fail=false,balanceCalls=0;
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.style={};this.isConnected=true;this.visible=true;}
 append(...items){this.children.push(...items)} replaceChildren(...items){this.children=items}
 setAttribute(){} getClientRects(){return this.visible?[{}]:[]}
}
const make=tag=>{const e=new Element(tag);elements.push(e);return e};
const text=(tag,value)=>{const e=make(tag);e.textContent=value;return e};
const document={createElement:make,hidden:false};
const context={document,text,actions:()=>make('div'),
 button:(label,fn)=>{const e=text('button',label);e.click=fn;return e},
 window:{setTimeout:(fn,delay)=>{timers.set(++seq,{fn,delay});return seq},clearTimeout:id=>timers.delete(id)},
 requestSetup:async(path,data)=>{
  if(data.action==='balance'){balanceCalls++;if(fail)throw Error('offline');return {billing_mode:'money',remaining_yuan:amount,used_yuan:'0.5'}}
  return {order:null};
 },drawUnifiedStatement:()=>{}
};
vm.runInNewContext(fs.readFileSync(0,'utf8')+';globalThis.mount=mountRelayBalance;',context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const tick=async()=>{const [id,t]=[...timers].find(([id,t])=>t.delay===15000);timers.delete(id);await t.fn();await flush()};
(async()=>{
 const panel=make('main');context.mount(panel);await flush();const box=panel.children[0];
 assert.equal(balanceCalls,1);assert.ok(elements.some(e=>e.textContent==='¥10.0000'));
 amount='9.95';await tick();assert.equal(balanceCalls,2);assert.ok(elements.some(e=>e.textContent==='¥9.9500'));
 document.hidden=true;await tick();assert.equal(balanceCalls,2);document.hidden=false;
 box.visible=false;await tick();assert.equal(balanceCalls,2);box.visible=true;
 fail=true;await tick();assert.equal(balanceCalls,3);assert.ok(elements.some(e=>e.textContent==='¥9.9500'));
 assert.equal([...timers.values()].filter(t=>t.delay===15000).length,1);
 fail=false;box.isConnected=false;await tick();assert.equal(balanceCalls,3);
 assert.equal([...timers.values()].filter(t=>t.delay===15000).length,0);
 amount='9.9';context.mount(make('main'));await flush();assert.equal(balanceCalls,4);
 assert.ok(elements.some(e=>e.textContent==='¥9.9000'));
 assert.equal([...timers.values()].filter(t=>t.delay===15000).length,1);
})().catch(e=>{console.error(e.stack);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', harness], input=source.encode(), capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
