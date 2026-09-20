import json
import shutil
import subprocess

import pytest

from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT as SOURCE


def test_selected_channel_keeps_other_channel_add_action(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node required for settings interaction test')
    choice = SOURCE[SOURCE.index('const chooseChannel ='):SOURCE.index('const renderWechat =')]
    refresh = SOURCE[SOURCE.index('async function refresh('):SOURCE.index('    refresh(false);')]
    script = r'''
const assert = require('node:assert/strict');
let state, actions, posts, rendered;
const STATUS='status', CHANNEL_CHOICE='choice', WECHAT_START='wechat-start';
const node=(_tag,text)=>({text,children:[],append(...items){this.children.push(...items)}});
const document={createDocumentFragment:()=>node('fragment')};
const content={replaceChildren(fragment){rendered=fragment}};
const renderWechat=()=>node('div','wechat-card'), renderQQ=()=>node('div','qq-card');
const action=(label,callback)=>{const button={label,callback};actions.push(button);return button};
const schedule=()=>{};
const renderError=(_parent,error)=>{throw error};
async function request(path,options){
  if(path===STATUS)return state;
  posts.push([path,options.body]);
  return {};
}
''' + choice + refresh + r'''
(async()=>{
  for(const [selected,label,expectedPosts] of [
    [['wechat'],'添加 QQ',[['choice',{choice:'qq'}]]],
    [['qq'],'添加微信',[['choice',{choice:'wechat'}],['wechat-start',{}]]],
  ]){
    state={selected_channels:selected,contact_state:selected[0],configured:{wechat:true}};
    actions=[];posts=[];
    await refresh();
    assert.deepEqual(actions.map(a=>a.label),[label]);
    assert.equal(rendered.children[0].text,selected[0]+'-card');
    await actions[0].callback();
    assert.deepEqual(posts,expectedPosts);
  }
  state={selected_channels:['qq','wechat'],contact_state:'both'};actions=[];
  await refresh();assert.equal(actions.length,0);
  state={selected_channels:[],contact_state:'invited'};actions=[];
  await refresh();assert.deepEqual(actions.map(a=>a.label),['微信','QQ','两个都要']);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    path = tmp_path / 'channel-ui.cjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
