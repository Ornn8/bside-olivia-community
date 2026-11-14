import shutil
import subprocess
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_topup_is_paid_on_afdian_with_the_accounts_own_code_and_the_credit_is_announced():
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
const elements=[];let credited=false;
const make=tag=>{const e=new Element(tag);elements.push(e);return e};
const text=(tag,value)=>{const e=make(tag);e.textContent=value;return e};
const calls=[],copied=[];
const link=n=>'https://ifdian.net/order/create?plan_id=p'+n+'&remark=K7M2QX9';
const context={document:{createElement:make},text,actions:()=>make('div'),
 URL,apiBase:'http://127.0.0.1:27149/',
 button:(label,fn)=>{const e=text('button',label);e.click=fn;return e},
 window:{setTimeout:()=>1,clearTimeout:()=>{}},
 navigator:{clipboard:{writeText:async value=>{copied.push(value)}}},
 requestSetup:async(path,data)=>{calls.push(data.action);
  if(data.action==='balance')return{billing_mode:'money',remaining_yuan:credited?'10.00000000':'0.00000000',used_yuan:'0'};
  if(data.action==='afdian')return{channel:'afdian',code:'K7M2QX9',page:'https://ifdian.net/a/x',plans:[
    {cents:1000,name:'一小碗白饭',link:link(10)},{cents:2000,name:'一大碗白饭',link:link(20)}]};
  return{}}
};
vm.runInNewContext(fs.readFileSync(0,'utf8')+';globalThis.mount=mountRelayBalance;',context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 const panel=make('main');context.mount(panel);
 await settle();
 assert.ok(!calls.includes('afdian')&&!calls.includes('order_status')&&!calls.includes('create_order')); // nothing until 充值 is opened
 elements.find(e=>e.tag==='button'&&e.textContent==='充值').click();
 await settle();
 assert.ok(calls.includes('afdian'));
 assert.ok(elements.some(e=>e.textContent==='K7M2QX9'));
 const links=elements.filter(e=>e.tag==='a');
 assert.deepEqual(links.map(e=>e.href),[link(10),link(20)]);
 assert.ok(links.every(e=>e.target==='_blank'&&e.rel==='noopener noreferrer'));
 assert.ok(elements.some(e=>e.textContent==='充 ¥10 → 选「一小碗白饭」'));
 assert.ok(!elements.some(e=>e.tag==='img'||/微信/.test(e.textContent||'')));
 elements.find(e=>e.tag==='button'&&e.textContent==='复制匹配码').click();
 elements.filter(e=>e.tag==='button'&&e.textContent==='复制链接')[1].click();
 await settle();
 assert.deepEqual(copied,['K7M2QX9',link(20)]);
 credited=true;
 elements.find(e=>e.tag==='button'&&e.textContent==='刷新').click();
 await settle();
 assert.ok(elements.some(e=>e.textContent==='可用余额 ¥10.0000 · 累计消费 ¥0.0000'));
 assert.ok(elements.some(e=>e.textContent==='充值已到账 ¥10.00。'));
})().catch(e=>{console.error(e.stack);process.exitCode=1});
'''
    result = subprocess.run([node,'-e',harness],input=source.encode(),capture_output=True,timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
