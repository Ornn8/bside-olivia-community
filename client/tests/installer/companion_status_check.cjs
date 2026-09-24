const fs=require('fs'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(process.argv[2],'utf8');
const code=source.slice(source.indexOf('  const setDiagnosticDetails ='), source.indexOf('  const memoryClearFailureMessage ='))+
 source.slice(source.indexOf('  const renderCompanionStatus ='), source.indexOf('  const scheduleMemoryStatusRefresh ='))+
 source.slice(source.indexOf('  const loadDialogData ='), source.indexOf('  const isSettingsRoute ='));
const calls=[];
class Element {constructor(){this.children=[];}append(...items){for(const item of items){item.parentElement=this;this.children.push(item)}}}
const context={STATUS_PATH:'status',window:{addEventListener:()=>{}},
 document:{createElement:()=>new Element()},text:(_tag,value)=>Object.assign(new Element(),{textContent:value}),
 renderLlmSetupPanel:async()=>{},renderCapabilityPanel:async()=>{},renderLocalUpdatePanel:()=>{},
 renderMemoryPanel:async(p,c)=>calls.push(['memory',c]),
 renderPrivateWorldPanel:async(p,c)=>calls.push(['world',c]),
 renderUnavailable:()=>{throw Error('healthy panels must not be disabled');},
 requestJson:async()=>({status:'UNAVAILABLE',capabilities:{memory:{state:'unavailable',reason_code:'MEM0_SEARCH_TIMEOUT'},private_world:{state:'available'}}})};
vm.createContext(context);vm.runInContext(code+';globalThis.load=loadDialogData;',context);
(async()=>{
 const parent=new Element(),status=Object.assign(new Element(),{dataset:{}});parent.append(status);
 await context.load(status,{},false);
 assert(!status.textContent.includes('MEM0_SEARCH_TIMEOUT'),'machine codes belong in collapsed details');
 assert(status.textContent.includes('尚未准备好'));
 assert.equal(status.__oliviaDiagnosticDetails.__oliviaCodeNode.textContent,'MEM0_SEARCH_TIMEOUT');
 assert.equal(status.__oliviaDiagnosticDetails.hidden,false);
 assert(status.textContent.includes('已连接'));
 assert.equal(status.dataset.state,'degraded');
 assert.equal(calls.find(c=>c[0]==='world')[1].state,'available');
 context.requestJson=async()=>{const e=Error();e.name='AbortError';throw e;};
 await context.load(status,{},false);
 assert(status.textContent.includes('超时'));
 console.log('companion degraded/timeout UI passed');
})().catch(e=>{console.error(e);process.exit(1);});
