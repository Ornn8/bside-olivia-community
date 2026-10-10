import json
import shutil
import subprocess

import pytest

from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT as SOURCE


def test_settings_offer_only_qq_without_mutating_existing_wechat_binding(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node required for settings interaction test')
    choice = SOURCE[SOURCE.index('const chooseChannel ='):SOURCE.index('const renderWechat =')]
    refresh = SOURCE[SOURCE.index('async function refresh('):SOURCE.index('    refresh(false);')]
    script = r'''
const assert = require('node:assert/strict');
let state, actions, posts, rendered;
let qqSaving=false, renderedStatus='', pollTimer;
const root={isConnected:true};
const updateReplyHealth=()=>{};
const STATUS='status', CHANNEL_CHOICE='choice', WECHAT_START='wechat-start';
const node=(_tag,text)=>({text,children:[],append(...items){this.children.push(...items)}});
const document={createDocumentFragment:()=>node('fragment')};
const content={contains:()=>false,replaceChildren(fragment){rendered=fragment}};
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
  for(const selected of [[],['wechat'],['qq'],['qq','wechat']]){
    state={selected_channels:selected,contact_state:selected[0],configured:{wechat:true}};
    const before=JSON.stringify(state);
    actions=[];posts=[];
    await refresh();
    assert.equal(JSON.stringify(state),before);
    assert.ok(!JSON.stringify(rendered).includes('wechat-card'));
    if(selected.includes('qq')){
      assert.equal(rendered.children[0].text,'qq-card');
      assert.equal(actions.length,0);
    }else{
      assert.deepEqual(actions.map(a=>a.label),['QQ']);
      await actions[0].callback();
      assert.deepEqual(posts,[['choice',{choice:'qq'}]]);
    }
  }
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    path = tmp_path / 'channel-ui.cjs'
    path.write_text(script, encoding='utf-8')
    result = subprocess.run([node, str(path)], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
