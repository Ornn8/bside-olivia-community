import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


@pytest.mark.parametrize(
    "scenario",
    [
        "same", "changed", "inflight", "save_body", "catalog", "qwen", "qwen-saved",
        "deepseek-v1",
    ],
)
def test_generated_llm_panel_saves_only_tested_normalized_configuration(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    source = BOOTSTRAP_JAVASCRIPT.split("  const setupInput =", 1)[1].split("  const formatBytes =", 1)[0]
    source = "const setupInput =" + source
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
let resolveTest;
let catalogFails=false;
const sent=[];
const context={
  document:{createElement:make}, text, actions:()=>make('div'),
  button:(label,fn)=>{const el=text('button',label);el.click=fn;return el;},
  setButtonsBusy:(buttons,busy)=>buttons.forEach(el=>{el.disabled=busy;el.style.opacity=busy?'0.5':'1';}),
  SETUP_STATUS_PATH:'status', LLM_TEST_PATH:'test', LLM_SAVE_PATH:'save', LLM_DELETE_PATH:'delete',
  requestSetup:async(path,body)=>{
    if(path==='status') {
      const savedQwen = process.argv[1]==='qwen-saved';
      const savedDeepseekV1 = process.argv[1]==='deepseek-v1';
      return {llm:{
        base_url:savedQwen?'https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1':savedDeepseekV1?'https://api.deepseek.com/v1/':'https://api.deepseek.com',
        model:savedQwen?'qwen3.8-flash':savedDeepseekV1?'deepseek-v4-flash':'model',key_configured:false,
      }};
    }
    sent.push({path,body});
    if(path==='/toy/setup/llm/models') {
      if(catalogFails) throw new Error('synthetic outage');
      return {models:['deepseek-v4-pro','deepseek-v4-flash']};
    }
    if(path==='test') return await new Promise(resolve=>resolveTest=resolve);
    return {status:'SAVED'};
  },
};
vm.runInNewContext(fs.readFileSync(0,'utf8')+';globalThis.render=renderLlmSetupPanel;',context);
(async()=>{
  await context.render(make('panel'));
  const inputs=elements.filter(el=>el.tag==='input');
  const [base,model,key]=inputs;
  const test=elements.find(el=>el.textContent==='测试连接');
  const save=elements.find(el=>el.textContent==='保存');
  const status=elements.find(el=>el.tag==='p' && el.textContent.startsWith('请先测试连接。'));
  assert.ok(status);
  key.value=' synthetic-key ';
  if(process.argv[1]==='qwen' || process.argv[1]==='qwen-saved') {
    const providers=elements.filter(el=>el.tag==='select')[0];
    const modelSelect=elements.filter(el=>el.tag==='select')[1];
    assert.ok(providers.children.some(el=>el.value==='qwen'));
    if(process.argv[1]==='qwen') {
      providers.value='qwen';providers.listeners.change();
      assert.equal(base.value,'https://dashscope.aliyuncs.com/compatible-mode/v1');
      assert.equal(model.value,'qwen3.8-max');
    } else {
      assert.equal(providers.value,'qwen');
      assert.equal(model.value,'qwen3.8-flash');
    }
    assert.equal(model.hidden,true);
    assert.equal(modelSelect.value,model.value);
    assert.deepEqual(modelSelect.children.map(el=>el.value),['qwen3.8-max','qwen3.8-flash']);
    modelSelect.value='qwen3.8-flash';modelSelect.listeners.change();
    assert.equal(model.value,'qwen3.8-flash');
    base.value='https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1';
    base.listeners.change();
    assert.equal(base.value,'https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1');
    return;
  }
  if(process.argv[1]==='deepseek-v1') {
    const providers=elements.filter(el=>el.tag==='select')[0];
    const modelSelect=elements.filter(el=>el.tag==='select')[1];
    assert.equal(providers.value,'deepseek');
    assert.equal(model.hidden,true);
    assert.equal(modelSelect.value,'deepseek-v4-flash');
    return;
  }
  if(process.argv[1]==='catalog') {
    const refresh=elements.find(el=>el.textContent==='刷新模型列表');
    const select=elements.filter(el=>el.tag==='select')[1];
    await refresh.click();
    assert.equal(model.hidden,true);
    assert.equal(select.value,'model');
    assert.deepEqual(select.children.map(el=>el.value),['model','deepseek-v4-pro','deepseek-v4-flash']);
    select.value='deepseek-v4-flash';select.listeners.change();
    assert.equal(model.value,'deepseek-v4-flash');
    catalogFails=true;await refresh.click();
    assert.equal(select.value,'deepseek-v4-flash');
    assert.ok(select.children.some(el=>el.value==='deepseek-v4-pro'));
    base.value='http://127.0.0.1:8000/v1';await refresh.click();
    assert.equal(model.hidden,false);assert.equal(select.hidden,true);
    return;
  }
  const pending=test.click();
  if(process.argv[1]==='inflight') {model.value='changed';model.listeners.input();}
  resolveTest({status:'AVAILABLE'});await pending;
  if(process.argv[1]==='inflight') {
    assert.equal(save.disabled,true);assert.match(status.textContent,/重新测试/);return;
  }
  assert.equal(save.disabled,false);
  if(process.argv[1]==='same') {
    key.value='synthetic-key';key.listeners.input();
    base.value=' https://api.deepseek.com ';base.listeners.input();
    model.listeners.input();
    assert.equal(save.disabled,false);assert.match(status.textContent,/可以保存/);
  } else if(process.argv[1]==='changed') {
    key.value='other-synthetic-key';key.listeners.input();
    assert.equal(save.disabled,true);assert.match(status.textContent,/重新测试/);
    await save.click();
    assert.equal(sent.filter(call=>call.path==='save').length,0);
  } else {
    await save.click();
    const request=sent.find(call=>call.path==='save');
    assert.deepEqual(request.body,sent.find(call=>call.path==='test').body);
    assert.equal(save.disabled,true);assert.equal(key.value,'');
  }
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", harness, scenario], input=source.encode("utf-8"), capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
