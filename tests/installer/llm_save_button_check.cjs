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
let saves=0;
const context={document:{createElement:tag=>new Element(tag)},
 text:(tag,value)=>Object.assign(new Element(tag),{textContent:value}),actions:()=>new Element('div'),
 SETUP_STATUS_PATH:'status',LLM_TEST_PATH:'test',LLM_SAVE_PATH:'save',LLM_DELETE_PATH:'delete',
 requestSetup:async(path)=>{if(path==='save')saves++; return {llm:{base_url:'http://127.0.0.1:19000/v1',model:'fixture',key_configured:false}};}};
vm.createContext(context);
vm.runInContext(section('  const button =','  const confirmAction =')+section('  const setupInput =','  const formatBytes =')+'\nglobalThis.render=renderLlmSetupPanel;',context);
(async()=>{
 const panel=new Element('div'); await context.render(panel,true);
 const all=e=>[e,...e.children.flatMap(all)];
 const test=all(panel).find(e=>e.textContent==='测试连接');
 const save=all(panel).find(e=>e.textContent==='保存');
 await test.click();
 console.log(JSON.stringify({disabled:save.disabled,opacity:save.style.opacity,cursor:save.style.cursor}));
 await save.click(); assert.equal(saves,1,'save click must reach endpoint');
 await test.click();
 assert.equal(save.style.opacity,'1','successful test must restore enabled appearance');
 assert.equal(save.style.cursor,'pointer');
})().catch(e=>{console.error(e.message);process.exitCode=1});
