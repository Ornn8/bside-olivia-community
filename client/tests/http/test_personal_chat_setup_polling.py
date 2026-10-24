import shutil
import subprocess

import pytest

from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT


def test_connected_poll_updates_error_without_replacing_focused_input_and_stops_off_page():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    source = PERSONAL_CHAT_SETUP_JAVASCRIPT.split('    const schedule =', 1)[1].split('    refresh(false);', 1)[0]
    harness = r'''
const assert=require('node:assert/strict');
let pollTimer=null, qqSaving=false, renderedStatus=null, calls=0, timer, delay;
const root={isConnected:true};
const field={value:'unsaved-owner',selectionStart:3};
const document={activeElement:field};
const state={textContent:''};
const health={children:[],replaceChildren(){this.children=[];},append(value){this.children.push(value);}};
const content={contains:x=>x===field,querySelector:q=>q.includes('live-state')?state:health,
 replaceChildren(){throw Error('focused input was replaced');}};
const STATUS='status',NAPCAT_LOGIN='login';
const node=(tag,value)=>({textContent:value});
const stateLabel=value=>value;
const renderError=(box,error)=>box.append({code:error.code});
let status={selected_channels:['qq'],listeners:{qq:'CONNECTED'},qq:{state:'CONNECTED'},
 reply_errors:{qq:'PERSONAL_CHAT_GENERATION_FAILED'},reply_error_at:{qq:'2026-09-29T21:11:07+08:00'}};
const request=async()=>{calls++;return status;};
const setTimeout=(fn,ms)=>{timer=fn;delay=ms;return 1;},clearTimeout=()=>{};
'''
    harness += 'const schedule =' + source
    harness += r'''
(async()=>{
 await refresh(true);
 assert.equal(calls,1); assert.equal(delay,10000);
 assert.equal(health.children[1].code,'PERSONAL_CHAT_GENERATION_FAILED');
 assert.match(health.children[2].textContent,/那条）再发一条消息/);
 assert.equal(field.value,'unsaved-owner');assert.equal(field.selectionStart,3);
 status={...status,reply_errors:{}};
 await refresh(true);assert.equal(health.children.length,0);
 root.isConnected=false; await timer(); assert.equal(calls,2);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', harness], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
