import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT

DIAGNOSTIC_HELPER = 'const setDiagnosticDetails =' + BOOTSTRAP_JAVASCRIPT.split(
    '  const setDiagnosticDetails =', 1)[1].split('  const memoryClearFailureMessage =', 1)[0]


def run_memory_browser(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    browser = "const createMemoryBrowser =" + BOOTSTRAP_JAVASCRIPT.split(
        "const createMemoryBrowser =", 1)[1].split("const renderPrivateWorldPanel =", 1)[0]
    harness = r'''
const assert = require('node:assert/strict');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.style={};this.value='';this.textContent='';
    this.dataset={};this.attributes={};this.events={};this.isConnected=true;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name]??null;}
  addEventListener(name,handler){this.events[name]=handler;}
  querySelectorAll(selector){
    const nodes=this.children.flatMap(child=>[child,...child.querySelectorAll('*')]);
    return nodes.filter(n=>selector==='*'||selector===n.tag||
      selector.startsWith('.')&&n.className?.split(' ').includes(selector.slice(1)));
  }
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  click(){return this.events.click?.();}
  focus(){}
}
const document={createElement:tag=>new Element(tag)},window={clearTimeout(){},setTimeout(){return 1;}};
const stateLabels={available:'AVAILABLE',degraded:'DEGRADED',unavailable:'UNAVAILABLE'};
const capabilityState=x=>x?.state||'unavailable',stack=()=>new Element('div'),actions=stack;
const text=(tag,value,className='')=>{const e=new Element(tag);e.textContent=value;e.className=className;return e;};
const button=(label,handler)=>{const e=text('button',label);e.addEventListener('click',handler);return e;};
const formatTime=x=>x, MEMORY_PATH='memory',STATUS_PATH='status';
const renderMemories=(list,rows)=>{for(const row of rows)list.append(text('p',row.text));};
let requestJson;
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const row=id=>({memory_id:id,text:id,source_id:'reply:fixture',created_at:null});
const page=id=>({memories:[row(id)],total:1,total_count:1,page:1,limit:20});
''' + DIAGNOSTIC_HELPER + browser + r'''
const makeBrowser=()=>{
  const panel=new Element('div'),summary=text('p',''),status=text('p','');
  const browser=createMemoryBrowser(panel,{state:'available'},summary,status,
    latest=>{summary.textContent=JSON.stringify(latest);});
  panel.append(browser.root);return {panel,summary,status,browser};
};
(async()=>{
''' + scenario + r'''
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, "-"], input=harness.encode("utf-8"), capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_old_search_response_cannot_overwrite_latest_query():
    run_memory_browser(r'''
const pending=[];requestJson=(_path,params)=>new Promise((resolve,reject)=>pending.push({resolve,reject,params}));
const {panel,browser,status}=makeBrowser();
const old=browser.load();panel.__oliviaMemoryBrowserState.memories.query='new';
const latest=browser.load();
assert.equal(pending[1].params.query,'new');
pending[1].resolve(page('new-result'));await latest;
pending[0].resolve(page('old-result'));await old;
assert.equal(browser.root.querySelector('.om-row-excerpt').textContent,'new-result');
const failed=browser.load(),newest=browser.load();
pending[3].resolve(page('newest-result'));await newest;const message=status.textContent;
pending[2].reject(Error('old failure'));await failed;
assert.equal(browser.root.querySelector('.om-row-excerpt').textContent,'newest-result');
assert.equal(status.textContent,message);
// Switching collections must also discard a pending memory response.
const abandoned=browser.load();
browser.root.querySelectorAll('button').find(e=>e.textContent==='信件原文').click();
pending[5].resolve({originals:[{source_id:'reply:original',speaker:'user',text:'原文结果',created_at:null,excerpt:false}],total:1,page:1,limit:20});
await settle();pending[4].resolve(page('wrong-tab'));await abandoned;
assert.equal(browser.root.querySelector('.om-row-excerpt').textContent,'原文结果');
''')


def test_original_results_remain_mounted_after_panel_finishes_loading():
    run_memory_browser(r'''
requestJson=async(_path,params)=>params?.collection==='originals'
 ? {indexed_letters:1,archive_total:1,archive_indexed:1,archive_removed:0,total:1,page:1,limit:20,
    originals:[{source_id:'reply:fixture',speaker:'user',text:'开心果冰淇淋。\n第二行',created_at:null,excerpt:false}]}
 : {memories:[],total:0,total_count:0,page:1,limit:20};
const panel=new Element('div');await renderMemoryPanel(panel,{state:'available',count:0});
const find=label=>panel.querySelectorAll('button').find(e=>e.textContent===label);
await find('信件原文').click();await settle();
assert.equal(panel.querySelector('.om-original-text').textContent,'开心果冰淇淋。\n第二行');
await find('记忆管理').click();
assert.match(panel.querySelector('.om-management').querySelectorAll('p')[0].textContent,/历史信件 1\/1/);
assert.ok(panel.querySelector('.om-original-text'),'management progress must keep the original mounted');
''')
