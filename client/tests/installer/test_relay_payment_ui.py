import shutil
import subprocess
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_pending_poll_preserves_payment_display_and_credits_face_value():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = 'const mountRelayBalance =' + BOOTSTRAP_JAVASCRIPT.split('  const mountRelayBalance =',1)[1].split('  const renderLlmSetupPanel =',1)[0]
    harness = r'''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.style={};this.attrs={};this.isConnected=true;}
 append(...items){this.children.push(...items)}
 replaceChildren(...items){this.children=items}
 setAttribute(k,v){this.attrs[k]=v}
}
const elements=[],timers=new Map();let sequence=0,credited=false;
const make=tag=>{const e=new Element(tag);elements.push(e);return e};
const text=(tag,value)=>{const e=make(tag);e.textContent=value;return e};
const calls=[];
const context={document:{createElement:make},text,actions:()=>make('div'),
 button:(label,fn)=>{const e=text('button',label);e.click=fn;return e},
 window:{setTimeout:(fn,delay)=>{timers.set(++sequence,{fn,delay});return sequence},clearTimeout:id=>timers.delete(id)},
 navigator:{clipboard:{writeText:async()=>{}}},
 requestSetup:async(path,data)=>{calls.push(data);return data.action==='balance'
 ?{billing_mode:'money',remaining_yuan:credited?'10.00000000':'0.00000000',used_yuan:'0'}
 :{server_time:1000,order:{id:'synthetic',amount_cents:999,credit_yuan:'10.00000000',expires_at:1300,state:credited?'credited':'pending'}}}
};
vm.runInNewContext(fs.readFileSync(0,'utf8')+';globalThis.mount=mountRelayBalance;',context);
(async()=>{
 const panel=make('main');context.mount(panel);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(elements.find(e=>e.tag==='select').value,'1000');
 const payment=elements.find(e=>e.textContent==='请用微信支付 ¥9.99');
 await [...timers.values()].find(t=>t.delay===3000).fn();
 assert.equal(elements.filter(e=>e.textContent==='请用微信支付 ¥9.99').length,1);
 assert.ok(panel.children[0].children.some(e=>e.children.includes(payment)));
 assert.equal([...timers.values()].filter(t=>t.delay===3000).length,1);
 credited=true;await [...timers.values()].find(t=>t.delay===3000).fn();
 assert.ok(elements.some(e=>e.textContent==='已到账 ¥10.00。'));
 assert.ok(elements.some(e=>e.textContent==='可用余额 ¥10.0000 · 累计消费 ¥0.0000'));
 assert.ok(calls.every(c=>!('paid' in c)));
})().catch(e=>{console.error(e.stack);process.exitCode=1});
'''
    result = subprocess.run([node,'-e',harness],input=source.encode(),capture_output=True,timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
