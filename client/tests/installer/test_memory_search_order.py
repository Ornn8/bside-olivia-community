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
    run_memory_browser(r"""
const pending=[];requestJson=(_path,params)=>new Promise((resolve,reject)=>pending.push({resolve,reject,params}));
const {panel,browser,status}=makeBrowser();
const old=browser.load();panel.__oliviaMemoryBrowserState.memories.query='new';
const latest=browser.load();
assert.equal(pending[1].params.query,'new');
assert.equal(pending[1].params.collection,'memories');
pending[1].resolve(page('new-result'));await latest;
pending[0].resolve(page('old-result'));await old;
assert.equal(browser.root.querySelector('.om-row-excerpt').textContent,'new-result');
const failed=browser.load(),newest=browser.load();
pending[3].resolve(page('newest-result'));await newest;const message=status.textContent;
pending[2].reject(Error('old failure'));await failed;
assert.equal(browser.root.querySelector('.om-row-excerpt').textContent,'newest-result');
assert.equal(status.textContent,message);
// A response that arrives after the panel left the window must not draw anything.
const abandoned=browser.load();panel.isConnected=false;
pending[4].resolve(page('after-detach'));await abandoned;
assert.equal(browser.root.querySelector('.om-row-excerpt').textContent,'newest-result');
""")


def test_browser_lists_memories_only_and_hands_the_source_letter_over():
    run_memory_browser(r"""
const requests=[];requestJson=async(_path,params)=>{requests.push(params);return page('fixture-memory');};
const opened=[];
const panel=new Element('div'),summary=text('p',''),status=text('p','');
const browser=createMemoryBrowser(panel,{state:'available'},summary,status,()=>{},source=>opened.push(source));
panel.append(browser.root);await browser.load();await settle();
assert.deepEqual(requests.map(x=>x.collection),['memories'],'the original letter text is read in the letter tab, not here');
assert.equal(browser.root.querySelectorAll('button').filter(e=>e.className==='om-tab').length,0,'no collection tabs');
const view=browser.root.querySelectorAll('button').find(e=>e.textContent==='查看关联原文');
assert.ok(view);view.click();assert.deepEqual(opened,['reply:fixture']);
// without a hand-over target the button is not offered
const bare=makeBrowser();await bare.browser.load();await settle();
assert.equal(bare.browser.root.querySelectorAll('button').find(e=>e.textContent==='查看关联原文'),undefined);
""")


def test_management_progress_opens_under_more_and_keeps_the_list_mounted():
    run_memory_browser(r"""
requestJson=async(_path,params)=>params?.collection==='originals'
 ? {indexed_letters:1,archive_total:1,archive_indexed:1,archive_removed:0,total:1,page:1,limit:20,originals:[]}
 : {memories:[row('kept-record')],total:1,total_count:1,page:1,limit:20};
const panel=new Element('div');await renderMemoryPanel(panel,{state:'available',count:1});
const find=label=>panel.querySelectorAll('button').find(e=>e.textContent===label);
assert.equal(find('记忆管理'),undefined,'the entry is called 更多 now');
assert.equal(panel.querySelector('.om-management').hidden,true,'collapsed until opened');
await find('更多').click();
assert.equal(panel.querySelector('.om-management').hidden,false);
assert.match(panel.querySelector('.om-management').querySelectorAll('p')[0].textContent,/历史信件 1\/1/);
assert.equal(panel.querySelector('.om-row-excerpt').textContent,'kept-record','management progress must keep the list mounted');
// the lifecycle controls moved here with the panel
for(const label of ['暂停长期记忆','清空当前用户记忆','重试未写入的记忆'])assert.ok(find(label),label);
""")


def test_settings_shows_a_launcher_instead_of_the_browser():
    run_memory_browser(r"""
let reads=0;requestJson=async()=>{reads++;return page('never-read');};
const opened=[];globalThis.openLettersAndMemories=async options=>{opened.push(options);};
const panel=new Element('div');await renderMemoryPanel(panel,{state:'available',count:7},{launcher:true});
assert.equal(reads,0,'the launcher reads no records');
assert.equal(panel.querySelector('.om-browser'),null);
const texts=panel.children.map(c=>c.textContent);
assert.ok(texts.some(x=>/AVAILABLE.*7/.test(x)),'the status line stays: '+texts.join('|'));
const open=panel.querySelectorAll('button').find(e=>e.textContent==='打开信件与记忆');
assert.ok(open);assert.equal(panel.querySelectorAll('button').length,1);
await open.click();assert.deepEqual(opened,[{tab:'memory'}]);
// a paused service is reported on the status line too
const paused=new Element('div');await renderMemoryPanel(paused,{state:'available',reason_code:'MEMORY_ADMIN_PAUSED'},{launcher:true});
assert.ok(paused.children.some(c=>/已暂停|Paused/.test(c.textContent)));
""")


def test_recovery_states_keep_their_own_controls_in_the_window_and_in_settings():
    run_memory_browser(r"""
requestJson=async()=>({capabilities:{memory:{state:'available'}}});
for(const options of [undefined,{launcher:true},{openOriginal(){}}]){
  const clear=new Element('div');
  await renderMemoryPanel(clear,{state:'unavailable',reason_code:'MEMORY_ADMIN_CLEAR_PENDING'},options);
  assert.ok(clear.querySelectorAll('button').some(e=>e.textContent==='继续完成清空'));
  const retry=new Element('div');
  await renderMemoryPanel(retry,{state:'unavailable',reason_code:'MEMORY_OUTBOX_RETRY_EXHAUSTED'},options);
  assert.ok(retry.querySelectorAll('button').some(e=>e.textContent==='重新准备长期记忆'));
  const embedding=new Element('div');
  await renderMemoryPanel(embedding,{state:'unavailable',reason_code:'MEM0_EMBEDDING_CACHE_UNAVAILABLE',embedding:{state:'missing'}},options);
  assert.ok(embedding.querySelectorAll('button').some(e=>e.textContent==='导入记忆离线包'));
}
""")
