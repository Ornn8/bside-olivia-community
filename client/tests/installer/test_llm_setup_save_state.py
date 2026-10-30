import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


@pytest.mark.parametrize("scenario", ["stored-key", "failure", "initial"])
def test_generated_llm_panel_uses_only_the_secure_native_key_prompt(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    source = BOOTSTRAP_JAVASCRIPT.split("  const renderLlmSetupPanel =", 1)[1].split("  const formatBytes =", 1)[0]
    source = "const renderLlmSetupPanel =" + source
    harness = r'''
const vm=require('node:vm'), fs=require('node:fs'), assert=require('node:assert/strict');
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.style={}; this.value=''; this.listeners={}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  setAttribute() {}
  addEventListener(event, fn) { this.listeners[event]=fn; }
}
const elements=[];
const make=tag=>{const el=new Element(tag);elements.push(el);return el;};
const text=(tag,value)=>{const el=make(tag);el.textContent=value;return el;};
const scenario=process.argv[1];
    let secureCalls=0;
const context={
  document:{createElement:make}, text, actions:()=>make('div'),
  button:(label,fn)=>{const el=text('button',label);el.click=fn;return el;},
  setButtonsBusy:(buttons,busy)=>buttons.forEach(el=>{el.disabled=busy;}),
  confirmAction:async()=>true,
  SETUP_STATUS_PATH:'status', LLM_DELETE_PATH:'delete',
      requestSetup:async(path,body)=>{
        if(path==='status') return {llm:{base_url:'https://175.24.191.6/v1',model:'qwen3.7-flash',key_configured:scenario!=='initial'}};
        throw new Error('unexpected renderer credential call');
      },
      window:{olivia:{connectAccount:async()=>{
        secureCalls++;
        if(scenario==='failure') throw Object.assign(new Error('synthetic'),{code:'RELAY_NOT_CONFIGURED'});
        return {connected:true};
      }}}
};
vm.runInNewContext(fs.readFileSync(0,'utf8')+';globalThis.render=renderLlmSetupPanel;',context);
(async()=>{
  await context.render(make('panel'), scenario==='initial');
  assert.equal(elements.filter(el=>el.tag==='select').length,0);
      const connect=elements.find(el=>el.textContent==='连接并保存');
  const remove=elements.find(el=>el.textContent==='删除 Key');
  assert.equal(remove.hidden,scenario==='initial');
      await connect.click();
      assert.equal(elements.filter(el=>el.tag==='input').length,0);
      assert.equal(secureCalls,1);
  const messages=elements.filter(el=>el.tag==='p').map(el=>el.textContent);
  if(scenario==='failure') {
    assert.ok(messages.some(m=>m.includes('请先在「Olivia 账户」获取 Key')));
    assert.equal(connect.disabled,false);
    return;
  }
      assert.ok(messages.some(m=>m.startsWith('已连接并保存 Olivia 回信服务')));
      assert.equal(remove.hidden,false);
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", harness, scenario], input=source.encode("utf-8"), capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
