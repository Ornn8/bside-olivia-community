import shutil
import subprocess
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


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
const renderMemories=(_list,rows)=>{displayed=rows[0].memory_id;};
const requestJson=(path,params)=>path===STATUS_PATH?Promise.resolve({capabilities:{}}):new Promise((resolve,reject)=>pending.push({resolve,reject}));
'''+load+'''
(async()=>{
const old=load();input.value='new';const latest=load();
pending[1].resolve({memories:[{memory_id:'new-result'}]});await latest;
pending[0].resolve({memories:[{memory_id:'old-result'}]});await old;
assert.equal(displayed,'new-result');
const failed=load();const newest=load();
pending[3].resolve({memories:[{memory_id:'newest-result'}]});await newest;
const message=resultState.textContent;
pending[2].reject(Error('old failure'));await failed;
assert.equal(displayed,'newest-result');assert.equal(resultState.textContent,message);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script],capture_output=True,timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf-8',errors='replace')
