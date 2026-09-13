import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_progress_can_close_and_reopen_without_submitting_again():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    source = 'const showLocalImportProgress =' + BOOTSTRAP_JAVASCRIPT.split(
        'const showLocalImportProgress =', 1)[1].split('const mountShell =', 1)[0]
    harness = r'''
const assert = require('node:assert/strict');
class Element {
  constructor() { this.children=[]; this.style={}; this.attrs={}; this.events={}; }
  append(...nodes) { for (const n of nodes) { n.remove(); n.parent=this; this.children.push(n); } }
  remove() { if(this.parent) this.parent.children=this.parent.children.filter(n=>n!==this); this.parent=null; }
  setAttribute(k,v) { this.attrs[k]=v; }
  addEventListener(k,v) { this.events[k]=v; }
  focus() {}
}
const body=new Element();
const find=(node,predicate)=>predicate(node)?node:node.children.map(n=>find(n,predicate)).find(Boolean);
const document={body,createElement:()=>new Element(),querySelector:selector=>find(body,n=>
  Object.hasOwn(n.attrs,selector.slice(1,-1)))};
const text=(tag,value)=>{const e=new Element();e.textContent=value;return e;};
const button=(label,callback)=>{const e=text('button',label);e.click=callback;return e;};
const setButtonsBusy=(buttons,busy)=>buttons.forEach(b=>b.disabled=busy);
const LOCAL_LETTER_IMPORT_PATH='import';
let mutations=0, confirmations=0, progressReads=0, release;
const confirmAction=async()=>{confirmations++;return true;};
const requestMutation=async()=>{mutations++;throw Error('must attach to existing import');};
const requestJson=async(path,query)=>query?.progress==='1'
  ? (++progressReads===1?{status:'RUNNING',stage:'memory',processed:1,total:3}
      :{status:'APPLIED',inserted:3})
  :{would_insert:3,would_update:0,would_remove:0};
const window={setTimeout:(callback,ms)=>{if(ms===2000)release=callback;},location:{reload(){}}};
'''
    harness += source + r'''
(async()=>{
  const section=new Element();body.append(section);mountLocalLetterImport(section);
  const entry=find(section,n=>n.textContent==='导入本地备份');
  const running=entry.click();
  for(let i=0;i<12;i++)await Promise.resolve();
  let modal=document.querySelector('[data-olivia-local-import-progress]');
  assert.ok(modal);assert.ok(!entry.disabled);
  modal.events.click({target:modal,preventDefault(){}});
  assert.ok(modal.parent,'outside click must preserve progress');
  find(modal,n=>n.textContent==='关闭进度窗口').click();
  assert.ok(!modal.parent);
  await entry.click();
  modal=document.querySelector('[data-olivia-local-import-progress]');
  assert.ok(modal);assert.equal(confirmations,1);assert.equal(mutations,0);
  assert.ok(find(modal,n=>n.textContent?.includes('1 / 3')));
  release();await running;
  assert.equal(progressReads,2);assert.equal(mutations,0);
  assert.ok(find(modal,n=>n.textContent?.includes('已导入 3 封')));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', harness], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
