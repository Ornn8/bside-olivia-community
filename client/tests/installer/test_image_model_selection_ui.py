import subprocess

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_image_model_chooser_uses_cloud_capability_and_keeps_old_server_behavior(tmp_path):
    source = BOOTSTRAP_JAVASCRIPT
    mount = source[source.index('  const mountVideoReplySetting ='):source.index('  const wardrobeStyle =')]
    harness = r'''
const assert=require('node:assert/strict');
let refreshVideoReplySetting=async()=>{};
const reportGroupStatus=()=>{};
const elements=[], requests=[];
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.style={};this.isConnected=true;this.value='';elements.push(this);}
 append(...nodes){this.children.push(...nodes);}
 set textContent(value){this._text=value;this.children=[];}
 get textContent(){return this._text;}
 setAttribute(key,value){this[key]=value;}
 addEventListener(event,fn){this.handlers[event]=fn;}
}
const document={createElement:tag=>new Element(tag)};
const text=(tag,value)=>{const node=new Element(tag);node.textContent=value;return node;};
const button=(label,fn)=>{const node=text('button',label);node.click=fn;return node;};
let state={tier:'audio',image:{enabled:true,resolution:'1K'}};
const routeRequest=async(path,body)=>{
 if(body){requests.push(body);state={...state,tier:body.tier,image:body.image};return {};}
 return state;
};
const videoReplyRequestId=()=> 'video_reply_setting:ui';
'''
    harness += mount + r'''
(async()=>{
 mountVideoReplySetting(new Element('section'));
 await new Promise(setImmediate);
 const models=elements.find(n=>n['aria-label']==='图片模型');
 const modelLabel=elements.find(n=>n.tag==='label'&&n.children.includes(models));
 const sizes=elements.find(n=>n['aria-label']==='图片分辨率');
 assert.equal(modelLabel.hidden,true);
 assert.deepEqual(sizes.children.map(n=>n.value),['1K','2K','4K']);
 state.image_capability={models:[
  {id:'small-photo',display_name:'Small Photo',resolutions:['1K','2K']},
  {id:'large-photo',display_name:'Large Photo',resolutions:['2K']}
 ],default_model:'small-photo'};
 await refreshVideoReplySetting();
 assert.equal(modelLabel.hidden,false);
 assert.deepEqual(models.children.map(n=>n.value),['','small-photo','large-photo']);
 assert.deepEqual(sizes.children.map(n=>n.value),['1K','2K']);
 models.value='large-photo';models.handlers.change();await new Promise(setImmediate);
 assert.equal(requests.length,1);
 assert.deepEqual(requests[0].image,{enabled:true,resolution:'2K',model:'large-photo'});
 assert.deepEqual(sizes.children.map(n=>n.value),['2K']);
 state.image_capability.models=state.image_capability.models.filter(n=>n.id!=='large-photo');
 await refreshVideoReplySetting();
 assert.equal(models.value,'large-photo');
 assert.equal(models.children.find(n=>n.value==='large-photo').disabled,true);
 assert.equal(requests.length,1); // A refresh never replaces a saved model.
 delete state.image_capability;state.image={enabled:true,resolution:'4K'};
 await refreshVideoReplySetting();
 assert.equal(modelLabel.hidden,true);
 assert.deepEqual(sizes.children.map(n=>n.value),['1K','2K','4K']);
 sizes.value='2K';sizes.handlers.change();await new Promise(setImmediate);
 assert.deepEqual(requests.at(-1).image,{enabled:true,resolution:'2K'});
  state.image_capability={models:[
   {id:'small-photo',display_name:'Small Photo',resolutions:['1K','2K']},
   {id:'local-image',display_name:'本地模型',resolutions:['1K','2K'],price_ranges_cents:{'1K':[10,20],'2K':[27,37]}},
   {id:'fixed-photo',display_name:'Fixed Photo',resolutions:['1K'],price_ranges_cents:{'1K':[15,15]}}
  ],default_model:'small-photo'};
  await refreshVideoReplySetting();
  assert.equal(models.value,'');
  assert.deepEqual(sizes.children.map(n=>n.textContent),['1K','2K']);
  const help=elements.find(n=>n.tag==='p'&&n.textContent?.startsWith('图片仅云端生成'));
  assert.ok(help.textContent.includes('每单小幅随机浮动'));
  assert.equal(models.children.find(n=>n.value==='local-image').textContent,'本地模型');
  models.value='local-image';models.handlers.change();await new Promise(setImmediate);
  assert.deepEqual(requests.at(-1).image,{enabled:true,resolution:'2K',model:'local-image'});
  assert.deepEqual(sizes.children.map(n=>n.textContent),['1K · ¥0.10–¥0.20','2K · ¥0.27–¥0.37']);
  assert.ok(help.textContent.includes('每张 ¥0.27–¥0.37，每单小幅随机浮动'));
  assert.ok(!help.textContent.includes('固定价'));
  sizes.value='1K';sizes.handlers.change();await new Promise(setImmediate);
  assert.ok(help.textContent.includes('每张 ¥0.10–¥0.20，每单小幅随机浮动'));
  assert.deepEqual(requests.at(-1).image,{enabled:true,resolution:'1K',model:'local-image'});
  models.value='fixed-photo';models.handlers.change();await new Promise(setImmediate);
  assert.deepEqual(sizes.children.map(n=>n.textContent),['1K · ¥0.15（固定）']);
  assert.ok(help.textContent.includes('固定价 ¥0.15 / 张'));
  assert.ok(!help.textContent.includes('小幅随机浮动'));
  models.value='';models.handlers.change();await new Promise(setImmediate);
  assert.equal(models.value,'');
  assert.deepEqual(sizes.children.map(n=>n.textContent),['1K','2K']);
  assert.ok(help.textContent.includes('费用按模型和分辨率确定，每单小幅随机浮动'));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    path = tmp_path / 'image-model-ui.cjs'
    path.write_text(harness, encoding='utf8')
    result = subprocess.run(['node', str(path)], capture_output=True, text=True, encoding='utf8')
    assert result.returncode == 0, result.stderr
