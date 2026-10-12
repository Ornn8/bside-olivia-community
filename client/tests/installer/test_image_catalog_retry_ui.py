import subprocess

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_catalog_retries_are_bounded_and_preserve_controls(tmp_path):
    source = BOOTSTRAP_JAVASCRIPT
    mount = source[source.index('  const mountVideoReplySetting ='):source.index('  const wardrobeStyle =')]
    harness = r'''
const assert=require('node:assert/strict');
let refreshVideoReplySetting;
const reportGroupStatus=()=>{},videoReplyRequestId=()=> 'video_reply_setting:test';
let elements=[],timers=new Map(),timerId=0,reads=0,fail=false,deferredRead=null;
const delays=[];
const setTimeout=(fn,delay)=>{delays.push(delay);timers.set(++timerId,fn);return timerId;};
const clearTimeout=id=>timers.delete(id);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;elements.push(this);}
 append(...nodes){this.children.push(...nodes);}
 set textContent(value){this._text=value;this.children=[];}
 get textContent(){return this._text;}
 setAttribute(key,value){this[key]=value;}
 addEventListener(event,fn){this.handlers[event]=fn;}
}
const document={createElement:tag=>new Element(tag)};
const text=(tag,value)=>{const node=new Element(tag);node.textContent=value;return node;};
const button=(label,fn)=>{const node=text('button',label);node.click=fn;return node;};
let state;
const routeRequest=async(path,body)=>{
 if(body){state={...state,tier:body.tier,image:body.image};return {};}
 reads++;if(fail)throw Error('offline');if(deferredRead)return deferredRead;return state;
};
const tick=()=>new Promise(setImmediate);
const fire=async()=>{assert.equal(timers.size,1);const [id,fn]=[...timers][0];timers.delete(id);await fn();};
const find=label=>elements.find(n=>n['aria-label']===label);
'''
    harness += mount + r'''
(async()=>{
 const start=async()=>{
  elements=[];timers.clear();delays.length=0;reads=0;fail=false;
  state={tier:'audio',image:{enabled:true,resolution:'1K'}};
  mountVideoReplySetting(new Element('section'));await tick();
 };
 await start();
 const sizes=find('图片分辨率'),models=find('图片模型');
 const pending=elements.find(n=>n.textContent==='正在读取可用的图片模型…');
 const container=elements.find(n=>n['data-olivia-reply-routes']);
 assert.ok(pending);assert.equal(sizes.disabled,false);
 await refreshVideoReplySetting();assert.equal(timers.size,1);
 fail=true;await fire();assert.equal(sizes.disabled,false);
 sizes.value='2K';sizes.handlers.change();await tick();assert.equal(state.image.resolution,'2K');
 await fire();await fire();
 assert.deepEqual(delays,[2000,5000,10000]);assert.equal(timers.size,0);assert.equal(pending.textContent,'');
 assert.equal(sizes.disabled,false);
 await start();await fire();
 state.image_capability={models:[{id:'photo-a',display_name:'Photo A',resolutions:['1K','2K']}],default_model:'photo-a'};
 await fire();assert.equal(timers.size,0);
 assert.equal(elements.find(n=>n.tag==='label'&&n.children.includes(find('图片模型'))).hidden,false);
 assert.deepEqual(delays,[2000,5000]);
 await start();
 state.image_capability={models:[{id:'photo-a',display_name:'Photo A',resolutions:['1K']} ]};
 await refreshVideoReplySetting();assert.equal(timers.size,0);
 await start();
 let release;
 const stale={...state,image:{...state.image},image_capability:{models:[{id:'photo-a',display_name:'Photo A',resolutions:['1K','2K']}]}};
 deferredRead=new Promise(resolve=>{release=resolve;});
 const retry=fire();await tick();
 const sizeDuringRead=find('图片分辨率');sizeDuringRead.value='2K';sizeDuringRead.handlers.change();await tick();
 release(stale);await retry;deferredRead=null;
 assert.equal(sizeDuringRead.value,'2K');assert.equal(timers.size,0);
 await start();
 elements.find(n=>n['data-olivia-reply-routes']).isConnected=false;
 const before=reads;await fire();assert.equal(reads,before);assert.equal(timers.size,0);
 // An initial settings failure also recovers without remounting.
 elements=[];timers.clear();fail=true;mountVideoReplySetting(new Element('section'));await tick();
 fail=false;await fire();assert.equal(find('图片分辨率').disabled,false);
 timers.clear();
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    path = tmp_path / 'image-catalog-retry.cjs'
    path.write_text(harness, encoding='utf8')
    result = subprocess.run(['node', str(path)], capture_output=True, text=True, encoding='utf8')
    assert result.returncode == 0, result.stderr
