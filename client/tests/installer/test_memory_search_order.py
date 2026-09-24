import shutil
import subprocess
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT

DIAGNOSTIC_HELPER = 'const setDiagnosticDetails =' + BOOTSTRAP_JAVASCRIPT.split(
    '  const setDiagnosticDetails =', 1)[1].split('  const memoryClearFailureMessage =', 1)[0]

def test_old_search_response_cannot_overwrite_latest_query():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    panel=BOOTSTRAP_JAVASCRIPT.split('const renderMemoryPanel =',1)[1]
    load=panel[panel.index('let memoryLoadGeneration'):panel.index('const search = button')]
    script='''
const assert=require('node:assert/strict');
const MEMORY_PATH='memory',STATUS_PATH='status',input={value:'old'},resultState={},list={replaceChildren(){}},pending=[];
let displayed;const updateSummary=()=>{};
const originalProgress={},originalPending=[],originals={replaceChildren(){},append(){}},text=()=>({}),formatTime=x=>x;
const renderMemories=(_list,rows)=>{displayed=rows[0].memory_id;};
const requestJson=(path,params)=>path===STATUS_PATH?Promise.resolve({capabilities:{}}):new Promise((resolve,reject)=>(params.collection==='originals'?originalPending:pending).push({resolve,reject}));
'''+DIAGNOSTIC_HELPER+load+'''
(async()=>{
const old=load();input.value='new';const latest=load();
pending[1].resolve({memories:[{memory_id:'new-result'}]});await latest;
originalPending[1].resolve({indexed_letters:84,archive_total:84,archive_indexed:84,archive_removed:0,originals:[]});await Promise.resolve();
const progress=originalProgress.textContent;
originalPending[0].resolve({indexed_letters:0,originals:[]});await Promise.resolve();
assert.equal(originalProgress.textContent,progress);
pending[0].resolve({memories:[{memory_id:'old-result'}]});await old;
assert.equal(displayed,'new-result');
const failed=load();const newest=load();
pending[3].resolve({memories:[{memory_id:'newest-result'}]});await newest;
const message=resultState.textContent;
pending[2].reject(Error('old failure'));await failed;
assert.equal(displayed,'newest-result');assert.equal(resultState.textContent,message);
originalPending[3].resolve({indexed_letters:85,originals:[]});await Promise.resolve();
const newestProgress=originalProgress.textContent;
originalPending[2].reject(Error('old original read failure'));await Promise.resolve();await Promise.resolve();
assert.equal(originalProgress.textContent,newestProgress);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script],capture_output=True,timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf-8',errors='replace')


def test_original_results_remain_mounted_after_panel_finishes_loading():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    panel=BOOTSTRAP_JAVASCRIPT.split('const renderMemoryPanel =',1)[1].split('const renderPrivateWorldPanel =',1)[0]
    script=r'''
const assert=require('node:assert/strict');
class Element {
  constructor(){this.children=[];this.style={};this.value='';this.textContent='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(){} addEventListener(){}
}
const document={createElement:()=>new Element()},window={clearTimeout(){}},stateLabels={available:'可用'};
const capabilityState=x=>x.state,stack=()=>new Element(),actions=stack;
const text=(tag,value)=>{const e=new Element();e.textContent=value;return e;};
const button=(label,handler)=>text('button',label),formatTime=x=>x;
const MEMORY_PATH='memory',STATUS_PATH='status';
const renderMemories=(list)=>list.append(text('p','暂无提取记忆'));
const requestJson=async(path,params)=>params?.collection==='originals'
 ? {indexed_letters:1,archive_total:1,archive_indexed:1,archive_removed:0,originals:[{speaker:'user',text:'开心果冰淇淋。\n第二行',created_at:null,excerpt:false}]}
 : path===STATUS_PATH?{capabilities:{memory:{state:'available',count:0}}}:{memories:[]};
'''+DIAGNOSTIC_HELPER+ 'const renderMemoryPanel ='+panel+r'''
(async()=>{
 const root=new Element();await renderMemoryPanel(root,{state:'available',count:0});
 const collect=e=>[e.textContent,...e.children.flatMap(collect)];
 const contents=collect(root);
 assert.ok(contents.includes('信件原文'));assert.ok(contents.includes('提取记忆'));
 assert.ok(contents.some(x=>x.includes('开心果冰淇淋')));
 assert.ok(contents.some(x=>x.includes('已接入 1/1')));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script],capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
