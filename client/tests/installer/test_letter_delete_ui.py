import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_delete_confirmation_restore_and_safe_text():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    source = 'const openLetterDeletion = ' + BOOTSTRAP_JAVASCRIPT.split('const openLetterDeletion = ', 1)[1].split('const mountMainNavigation =', 1)[0]
    harness = r'''
const assert=require('node:assert/strict'), all=[];
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.events={};this.style={};all.push(this)}
 append(...nodes){this.children.push(...nodes)} replaceChildren(...nodes){this.children=nodes}
 setAttribute(){} addEventListener(k,v){this.events[k]=v} showModal(){} focus(){} close(){} remove(){}
 querySelectorAll(){return this.children.flatMap(c=>[...(c.tag==='button'?[c]:[]),...c.querySelectorAll()])}
}
const document={createElement:tag=>new Element(tag),querySelector:()=>null,body:new Element('body')};
const window={location:{reload(){}}};
const text=(tag,value)=>Object.assign(new Element(tag),{textContent:value});
const button=(label,fn)=>Object.assign(text('button',label),{click:fn});
const actions=()=>new Element('div'),stack=actions;
let confirmed=false,deleted=false,requests=[];
const confirmAction=async message=>{assert.ok(message.includes('她关于这封信的记忆')||message.includes('会一起恢复'));return confirmed};
const routeRequest=async(path,body,options)=>{
 if(path.endsWith('/delete')){assert.equal(options.confirmed,true);requests.push(body);deleted=true;return {status:'DELETED'}}
 if(path.endsWith('/restore')){assert.equal(options.confirmed,true);requests.push(body);deleted=false;return {status:'RESTORED'}}
 if(path.includes('/deletion/list'))return {items:deleted?[{id:'trash',letter_id:'a',part:'user',status:'deleted',summary:'private',expires_at:1}]:[]};
 if(path.includes('/detail'))return {content:'<img src=x onerror=evil()>',reply_text:'她的回信'};
 return {list:[{letter_id:'a',summary:'private'}]};
};
'''
    harness += source + r'''
(async()=>{
 await openLetterDeletion();
 const find=label=>all.filter(el=>el.tag==='button'&&el.textContent===label).at(-1);
 await find('查看并删除').click();
 assert.ok(all.some(el=>el.textContent==='<img src=x onerror=evil()>'));
 await find('删除你的信').click();assert.equal(requests.length,0);
 confirmed=true;await find('删除你的信').click();assert.deepEqual(requests,[{letter_id:'a',part:'user'}]);
 await find('最近删除').click();await find('恢复这轮信件和记忆').click();
 assert.deepEqual(requests.at(-1),{id:'trash'});
 await find('返回信箱').click();await find('查看并删除').click();
 await find('删除她的信').click();assert.equal(requests.at(-1).part,'reply');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', harness], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf8', errors='replace')
