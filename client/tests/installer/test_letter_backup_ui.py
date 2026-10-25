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
const confirmations=[];
const window={setTimeout(){},location:{reload(){}}},confirmAction=async(value)=>{confirmations.push(value);return true};
const URL={createObjectURL(value){blob=value;return 'blob:fixture'},revokeObjectURL(){}};
let backup={schema_version:'olivia.letters.v1',letters:[
 {content:'line one\nline two',reply_text:'reply'},
 {content:'qq message',reply_text:'qq response',channel:'qq',delivery_status:'DELIVERED'},
 {content:'wechat message',reply_text:'',channel:'wechat',delivery_status:'RECEIVED_ONLY'},
 {content:'what he said on the call',reply_text:'what she said',title:'语音通话',source_id:'call-0123456789abcdef'}
],diary:{entries:[{day:'2026-10-25',body:'entry'},{day:'2026-10',body:'memoir'}],comments:[]},memories:[{text:'fact'}]};
let restored={status:'RESTORING',count:1};
const requestMutation=async(path,body)=>{
 if(path.endsWith('/export'))return {status:'READY',backup};
 imported=body.backup;return {status:'APPLIED',inserted:1,duplicates:0,diary_entries:2,memories:restored};
};
'''
    helper = 'const setDiagnosticDetails =' + BOOTSTRAP_JAVASCRIPT.split(
        '  const setDiagnosticDetails =', 1)[1].split('  const memoryClearFailureMessage =', 1)[0]
    harness += helper + source + r'''
(async()=>{
 mountLetterBackup(new Element());
 const buttons=all.filter(x=>x.textContent&&typeof x.click==='function');
 const save=buttons.find(x=>x.textContent==='导出备份');
 await save.click(); assert.match(downloaded,/^Olivia-letters-.*\.json$/);
 assert.deepEqual(JSON.parse(await blob.text()),backup);
 assert.ok(all.some(x=>x.textContent==='已导出 1 封信件、2 条聊天、1 段通话、2 篇日记和 1 条记忆，请在下载位置查看。'));
 const file=all.find(x=>x.type==='file');
 file.files=[{size:100,text:async()=>JSON.stringify(backup)}];
 await file.events.change(); assert.deepEqual(JSON.parse(imported),backup); assert.equal(file.value,'');
 assert.match(confirmations.at(-1),/聊天.*日记.*记忆/);
 assert.match(confirmations.at(-1),/只读.*不.*发/);
 assert.ok(all.some(x=>x.textContent==='已导入 1 条记录和 2 篇日记，重复 0 条。正在刷新历史记录。 1 条记忆正在后台恢复。'));
 backup={schema_version:'olivia.letters.v1',letters:[{content:'legacy letter',reply_text:'legacy reply'}],memories_unavailable:true};
 await save.click();assert.deepEqual(JSON.parse(await blob.text()),backup);
 assert.ok(all.some(x=>x.textContent==='已导出 1 封信件、0 条聊天、0 段通话、0 篇日记和 0 条记忆，请在下载位置查看。 长期记忆这次没读到，未包含在文件里，稍后可再导出一次。'));
 restored={status:'UNAVAILABLE',count:3};
 file.files=[{size:100,text:async()=>JSON.stringify(backup)}];
 await file.events.change(); assert.deepEqual(JSON.parse(imported),backup);
 assert.ok(all.some(x=>x.textContent==='已导入 1 条记录和 2 篇日记，重复 0 条。正在刷新历史记录。 长期记忆暂不可用，3 条记忆未恢复，可稍后再导入一次。'));
 restored=undefined;
 const pairs=[{content:'original',reply:'response'}];
 file.files=[{size:100,text:async()=>JSON.stringify(pairs)}];
 await file.events.change(); assert.deepEqual(JSON.parse(imported),pairs);
 assert.equal(save.disabled,false);
 assert.match(file.accept,/\.soul/);
 const manifest={memory:{exchanges:[{incoming:'hello',reply:'reply',date:'2026-09-30',time:'12:34',replyVideoUrl:'private-media'}]},videos:['private-video']};
 const bytes=new TextEncoder().encode(JSON.stringify(manifest)),head=new Uint8Array(16);
 head.set(new TextEncoder().encode('SOUL0001'));
 new DataView(head.buffer).setUint32(8,bytes.length,true);
 const reads=[];
 file.files=[{name:'history.SOUL',size:500*1024*1024,slice(start,end){
   reads.push([start,end]);
   assert.ok(end<=16+bytes.length,'must not read media');
   return new Blob([start===0?head:bytes]);
 },text(){throw Error('must not read whole soul')}}];
 await file.events.change();
 assert.equal(imported.format,'soul');
 assert.deepEqual(imported.manifest,{memory:{exchanges:[{incoming:'hello',reply:'reply',date:'2026-09-30',time:'12:34'}]}});
 assert.deepEqual(reads,[[0,16],[16,16+bytes.length]]);
 imported=null;
 file.files=[{name:'broken.soul',size:20,slice(){return new Blob([head])}}];
 await file.events.change();assert.equal(imported,null);assert.equal(save.disabled,false);
 imported=null;file.files=[{size:65*1024*1024,text:async()=>{throw Error('must not read oversized file')}}];
 await file.events.change();assert.equal(imported,null);assert.equal(save.disabled,false);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', harness], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf-8',errors='replace')
