import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


HARNESS = r'''
const assert=require('node:assert/strict');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.events={};this.style={};this.attrs={};this.className=''}
 append(...nodes){for(const n of nodes){n.parent=this;this.children.push(n)}}
 replaceChildren(...nodes){this.children=[];this.append(...nodes)}
 setAttribute(k,v){this.attrs[k]=v} addEventListener(k,v){this.events[k]=v}
 showModal(){this.open=true} focus(){} close(){this.open=false}
 remove(){this.parent.children=this.parent.children.filter(x=>x!==this)}
 querySelectorAll(selector){return this.children.flatMap(c=>[...(selector.split(',').includes(c.tag)?[c]:[]),...c.querySelectorAll(selector)])}
 querySelector(selector){return this.querySelectorAll(selector)[0]}
}
const document={createElement:tag=>new Element(tag),querySelector:()=>null,body:new Element('body')};
const window={location:{reload(){}}};
const text=(tag,value,cls)=>Object.assign(new Element(tag),{textContent:value,className:cls||''});
const button=(label,fn)=>Object.assign(text('button',label),{click:fn});
const actions=()=>new Element('div');
// what the window takes from the surrounding script: the confirmation hook, the memory service and the memory tab
let confirmHost=null;const MEMORY_PATH='memory',STATUS_PATH='status';
let memoryMounts=[],requestJson=async()=>{throw new Error('no memory service')};
const mountMemoryTab=async(host,openOriginal)=>{memoryMounts.push({host,openOriginal})};
let requests=[],failAt=0,failLoad=false,hold=null,detailRequests=0,readHold=null;
let letters=[{letter_id:'a',content:'<img src=x onerror=evil()>',reply_text:'她的回信',created_at:1792230000},
 {letter_id:'b',content:'第二封信',reply_text:'第二封回信'},
 {letter_id:'c',origin:'proactive',content:'内部提示',reply_text:'她主动寄来的信'}];
let trash=[];
const routeRequest=async(path,body,options)=>{
 if(path.endsWith('/delete')){
  assert.equal(options.confirmed,true);requests.push(body);if(hold)await hold;
  if(failAt===requests.length)throw new Error('LETTER_DELETE_MEMORY_BUSY');
  const row=letters.find(x=>x.letter_id===body.letter_id),field=body.part==='user'?'content':'reply_text';
  trash.push({id:String(trash.length),letter_id:body.letter_id,part:body.part,summary:row[field],status:'deleted',expires_at:1794822000});row[field]='';return {};
 }
 if(path.endsWith('/restore')){assert.equal(options.confirmed,true);requests.push(body);trash=[];return {}}
 if(path.includes('/deletion/list'))return {items:trash};
 if(failLoad)throw new Error('unavailable');
 if(path.includes('/detail')){detailRequests++;if(readHold)await readHold;return letters.find(x=>x.letter_id===new URLSearchParams(path.split('?')[1]).get('letter_id'))}
 return {list:letters};
};
const all=()=>document.body.querySelectorAll('button,input,span,p,dialog,strong');
const find=label=>all().filter(el=>el.tag==='button'&&el.textContent===label).at(-1);
const inputs=()=>all().filter(el=>el.tag==='input'&&el.type==='checkbox');
const select=(index,value=true)=>{const el=inputs()[index];el.checked=value;el.events.change()};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const accept=async promise=>{await tick();await find('确定').click();await promise};
const expand=async()=>{for(const el of all().filter(x=>x.className==='lm-title'))await el.click()};
const openAll=async()=>{await openLettersAndMemories();await expand()};
'''


def run_js(checks):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    source = 'const openLettersAndMemories = ' + BOOTSTRAP_JAVASCRIPT.split('const openLettersAndMemories = ', 1)[1].split('const mountMainNavigation =', 1)[0]
    result = subprocess.run([node, '-e', HARNESS + source + '\n(async()=>{' + checks + '})().catch(error=>{console.error(error);process.exitCode=1});'], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf8', errors='replace')


def test_delete_confirmation_restore_and_safe_text():
    run_js(r'''
await openAll();
const title=all().find(x=>x.tag==='strong'&&x.textContent==='<img src=x onerror=evil()>');
await title.parent.click();assert.ok(all().some(x=>x.tag==='p'&&x.textContent===title.textContent));
select(0);const cancelled=find('删除所选').click();await tick();await find('取消').click();await cancelled;assert.equal(requests.length,0);
await accept(find('删除所选').click());assert.deepEqual(requests,[{letter_id:'a',part:'user'}]);
assert.equal(letters[0].reply_text,'她的回信');
await find('最近删除').click();await accept(find('恢复').click());assert.deepEqual(requests.at(-1),{id:'0'});
await find('信件').click();await expand();select(0);await accept(find('删除所选').click());assert.equal(requests.at(-1).part,'reply');
''')


def test_selection_count_deselection_and_batch_delete():
    run_js(r'''
await openAll();assert.equal(inputs().length,5);assert.equal(find('删除所选').disabled,true);
assert.ok(!all().some(x=>x.textContent==='内部提示'));assert.ok(all().some(x=>x.tag==='p'&&x.textContent==='她主动寄来的信'));
select(0);select(1);select(2);select(1,false);select(4);
assert.ok(all().some(x=>x.textContent==='已选 3 封'));
await accept(find('删除所选').click());
assert.deepEqual(requests,[{letter_id:'a',part:'user'},{letter_id:'b',part:'user'},{letter_id:'c',part:'reply'}]);
assert.equal(find('删除所选').disabled,true);assert.ok(all().some(x=>x.textContent==='已选 0 封'));
''')


def test_batch_stops_on_failure_and_keeps_unprocessed_letters():
    run_js(r'''
await openAll();select(0);select(2);select(4);failAt=2;
await accept(find('删除所选').click());assert.equal(requests.length,2);assert.equal(letters[1].content,'第二封信');assert.equal(letters[2].reply_text,'她主动寄来的信');
assert.ok(all().some(x=>x.textContent?.includes('已完成 1 / 3，后续操作已停止。记忆正在写入')));
assert.equal(find('关闭').disabled,false);
''')


def test_busy_prevents_duplicate_mutation_and_escape_close():
    run_js(r'''
await openAll();select(0);let release;hold=new Promise(r=>release=r);
const pending=find('删除所选').click();await tick();await find('确定').click();await tick();
assert.equal(find('关闭').disabled,true);assert.equal(inputs()[0].disabled,true);
await find('删除所选').click();await find('关闭').click();assert.equal(document.body.children.length,1);
assert.equal(requests.length,1);release();await pending;
''')


def test_empty_read_failure_and_tab_clear_selection():
    run_js(r'''
await openAll();select(0);await find('最近删除').click();assert.ok(all().some(x=>x.textContent==='最近没有删除的信件。'));
assert.equal(find('删除所选').disabled,true);letters=[];await find('信件').click();assert.ok(all().some(x=>x.textContent==='信箱里还没有信件。'));
await find('最近删除').click();failLoad=true;await find('信件').click();assert.ok(all().some(x=>x.textContent==='信箱读取失败，请关闭后重试。'));assert.equal(find('关闭').disabled,false);
''')


def test_prepared_retry_requires_confirmation():
    run_js(r'''
trash=[{id:'t',letter_id:'a',part:'user',summary:'未完成的删除',status:'prepared',expires_at:1}];
await openLettersAndMemories();await find('最近删除').click();
const cancelled=find('继续清理').click();await tick();await find('取消').click();await cancelled;assert.equal(requests.length,0);
await accept(find('继续清理').click());assert.deepEqual(requests,[{letter_id:'a',part:'user'}]);
''')


def test_open_does_not_mark_letters_read_and_pending_preview_can_close():
    run_js(r'''
await openLettersAndMemories();assert.equal(detailRequests,0);
let release;readHold=new Promise(r=>release=r);
const pending=all().find(x=>x.className==='lm-title').click();await tick();
assert.equal(detailRequests,1);assert.equal(find('关闭').disabled,false);
await find('关闭').click();assert.equal(document.body.children.length,0);
release();await pending;assert.equal(detailRequests,1);assert.equal(requests.length,0);assert.equal(document.body.children.length,0);
''')


def test_tabs_keep_the_ticks_and_mount_the_memory_tab_once():
    run_js(r'''
const panel=name=>document.body.querySelectorAll('div').find(x=>x.className.split(' ').includes(name));
await openAll();select(0);select(2);
assert.ok(all().some(x=>x.textContent==='已选 2 封'));
const listReads=()=>requests.length;
assert.equal(memoryMounts.length,0,'the memory tab is not read before it is shown');
await find('记忆').click();
assert.equal(memoryMounts.length,1);assert.equal(panel('lm-memory').hidden,false);assert.equal(panel('lm-panel').hidden,true);
assert.equal(typeof memoryMounts[0].openOriginal,'function');
await find('信件').click();
assert.equal(panel('lm-memory').hidden,true);assert.equal(panel('lm-panel').hidden,false);
assert.ok(all().some(x=>x.textContent==='已选 2 封'),'ticks survive a visit to the memory tab');
assert.equal(inputs().filter(x=>x.checked).length,2);
await find('记忆').click();assert.equal(memoryMounts.length,1,'mounted once');
await find('最近删除').click();assert.equal(panel('lm-memory').hidden,true);
assert.ok(all().some(x=>x.textContent==='最近没有删除的信件。'));
await find('信件').click();assert.ok(all().some(x=>x.textContent==='已选 0 封'),'the list reloads after the recently deleted tab; ticks are cleared as before');
''')


def test_the_window_can_open_on_the_memory_tab():
    run_js(r'''
const panel=name=>document.body.querySelectorAll('div').find(x=>x.className.split(' ').includes(name));
await openLettersAndMemories({tab:'memory'});
assert.equal(memoryMounts.length,1);assert.equal(panel('lm-memory').hidden,false);
const tabs=all().filter(x=>x.tag==='button'&&x.className==='lm-tab');
assert.deepEqual(tabs.map(x=>[x.textContent,x.attrs['aria-selected']]),[['信件','false'],['记忆','true'],['最近删除','false']]);
await find('信件').click();assert.equal(panel('lm-memory').hidden,true);
''')


def test_memory_confirmations_use_the_window_and_are_released_on_close():
    run_js(r'''
await openLettersAndMemories();
assert.equal(typeof confirmHost,'function','memory actions confirm inside the modal window');
const answer=confirmHost('确认清空当前用户的 Mem0 长期记忆？');await tick();
assert.ok(all().some(x=>x.tag==='p'&&x.textContent==='确认清空当前用户的 Mem0 长期记忆？'));
await find('取消').click();assert.equal(await answer,false);
const second=confirmHost('再确认一次？');await tick();await find('确定').click();assert.equal(await second,true);
await find('关闭').click();assert.equal(confirmHost,null);
''')


def test_tabs_are_locked_while_a_deletion_runs():
    run_js(r'''
await openAll();select(0);let release;hold=new Promise(r=>release=r);
const pending=find('删除所选').click();await tick();await find('确定').click();await tick();
for(const label of ['信件','记忆','最近删除'])assert.equal(find(label).disabled,true,label);
await find('记忆').click();assert.equal(memoryMounts.length,0);
release();await pending;
for(const label of ['信件','记忆','最近删除'])assert.equal(find(label).disabled,false,label);
''')
