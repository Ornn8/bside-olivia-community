const fs=require('fs'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(process.argv[2],'utf8');
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.style={};this.listeners={};this.disabled=false;this.value='';}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(){}
  addEventListener(name,fn){this.listeners[name]=fn;}
  async click(){if(!this.disabled) await this.listeners.click();}
}
const section=(from,to)=>source.slice(source.indexOf(from),source.indexOf(to));
const helpers=section('  const button =','  const confirmAction =')+section('  const setupInput =','  const renderLlmSetupPanel =');

async function run(native) {
 const secure=[];
 const relay=[];
 const context={document:{createElement:tag=>new Element(tag)},
  t:(value)=>value,
  text:(tag,value)=>Object.assign(new Element(tag),{textContent:value}),actions:()=>new Element('div'),
  button:(label,fn)=>{const el=Object.assign(new Element('button'),{textContent:label});el.click=fn;return el;},
  setButtonsBusy:(buttons,busy)=>buttons.forEach((el)=>{el.disabled=busy;}),
  confirmAction:async()=>true,
  SETUP_STATUS_PATH:'status',LLM_DELETE_PATH:'delete',
  requestSetup:async(path,body)=>{
    if(path==='/toy/relay/action') relay.push(body);
    return {llm:{base_url:'https://175.24.191.6/v1',model:'qwen3.7-flash',key_configured:false}};
  },
  window:native ? {olivia:{connectAccount:async()=>{secure.push(true);return true;}}} : {}
 };
 vm.createContext(context);
 vm.runInContext(helpers+section('  const renderLlmSetupPanel =','  const formatBytes =')+'\nglobalThis.render=renderLlmSetupPanel;',context);
 const all=e=>[e,...e.children.flatMap(all)];
 const panel=new Element('div'); await context.render(panel,false);
 const connect=all(panel).find(e=>e.textContent==='连接并保存');
 const remove=all(panel).find(e=>e.textContent==='删除 Key');
 assert(!all(panel).some(e=>e.textContent==='测试连接' || e.textContent==='保存'),'only the Olivia connect action remains');
 assert.equal(remove.hidden,true,'no saved key means nothing to delete');
 if(native) {
   await connect.click();
   assert.deepEqual(secure,[true],'connect click must use the secure native prompt');
   assert.equal(all(panel).filter(e=>e.tagName==='input').length,0,'native entry has no paste field');
 } else {
   const key=all(panel).find(e=>e.tagName==='input');
   key.value=' olivia-synthetic ';
   await connect.click();
   assert.deepEqual(relay,[{action:'import_key',key:'olivia-synthetic'}],'Windows keeps the paste/import flow');
   assert.equal(key.value,'','successful import clears the paste field');
 }
 assert.equal(connect.disabled,false,'successful connection must restore the enabled button');
 assert.equal(remove.hidden,false,'a saved key can then be deleted');
 const initialPanel=new Element('div');await context.render(initialPanel,true);
 assert(all(initialPanel).some(e=>e.textContent==='连接并保存'),'first-run uses one connect-and-save action');
}

(async()=>{await run(true);await run(false);})().catch(e=>{console.error(e.message);process.exitCode=1});
