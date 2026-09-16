import shutil
import subprocess
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_backup_buttons_download_and_restore_selected_document():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    source = 'const mountHistoryRelationship =' + BOOTSTRAP_JAVASCRIPT.split(
        'const mountHistoryRelationship =', 1)[1].split('const mountLocalLetterImport =', 1)[0]
    harness = r'''
const assert=require('node:assert/strict');
const all=[];
class Element {
 constructor(){this.children=[];this.events={};all.push(this)}
 append(...nodes){this.children.push(...nodes)}
 setAttribute(){} addEventListener(k,v){this.events[k]=v} remove(){}
 click(){if(this.href)downloaded=this.download}
}
let downloaded='',imported=null,blob=null;
const document={body:new Element(),createElement:()=>new Element()};
const text=(tag,value)=>Object.assign(new Element(),{textContent:value});
const button=(label,callback)=>Object.assign(text('button',label),{click:callback});
const actions=()=>new Element(),setButtonsBusy=(buttons,busy)=>buttons.forEach(b=>b.disabled=busy);
const LOCAL_LETTER_IMPORT_PATH='local-import',requestJson=async()=>({status:'APPLIED',processed:0,total:0});
const window={setTimeout(){},location:{reload(){}}},confirmAction=async()=>true;
const URL={createObjectURL(value){blob=value;return 'blob:fixture'},revokeObjectURL(){}};
const backup={schema_version:'olivia.letters.v1',letters:[{content:'line one\nline two',reply_text:'reply'}]};
const requestMutation=async(path,body)=>{
 if(path.endsWith('/export'))return {status:'READY',backup};
 imported=body.backup;return {status:'APPLIED',inserted:1,duplicates:0};
};
'''
    harness += source + r'''
(async()=>{
 mountLetterBackup(new Element());
 const buttons=all.filter(x=>x.textContent&&typeof x.click==='function');
 const save=buttons.find(x=>x.textContent==='导出信件备份');
 await save.click(); assert.match(downloaded,/^Olivia-letters-.*\.json$/);
 assert.deepEqual(JSON.parse(await blob.text()),backup);
 const file=all.find(x=>x.type==='file');
 file.files=[{size:100,text:async()=>JSON.stringify(backup)}];
 await file.events.change(); assert.deepEqual(JSON.parse(imported),backup); assert.equal(file.value,'');
 const pairs=[{content:'original',reply:'response'}];
 file.files=[{size:100,text:async()=>JSON.stringify(pairs)}];
 await file.events.change(); assert.deepEqual(JSON.parse(imported),pairs);
 assert.equal(save.disabled,false);
 imported=null;file.files=[{size:17*1024*1024,text:async()=>{throw Error('must not read oversized file')}}];
 await file.events.change();assert.equal(imported,null);assert.equal(save.disabled,false);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', harness], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf-8',errors='replace')
