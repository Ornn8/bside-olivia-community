"""Audio-only enhancement at the shipped native MIDI card locations."""

JAVASCRIPT = r'''
(() => {
  if(typeof document==='undefined'||typeof document.querySelector!=='function')return;
  const t=(zh,vars)=>window.oliviaI18n?window.oliviaI18n.t(zh,vars):String(zh).replace(/\{(\w+)\}/g,(m,k)=>vars&&k in vars?vars[k]:m);
  const marker=document.querySelector('[data-olivia-companion-settings]');
  const base=marker&&marker.getAttribute('data-api-base');if(!base)return;
  let caps=null,dialog=null,timer=null,busy=false;
  const original='.midi-upload-card,.midi-upload-card-large';
  const css=document.createElement('style');
  css.textContent=`${original}{display:none!important}
  .piano-entry{padding:16px;border:1px solid #c5b7ad;border-radius:12px;background:#faf5ee;color:#392f2a;width:100%;text-align:left;cursor:pointer}
  .piano-dialog{width:min(640px,90vw);max-height:85vh;overflow:auto;background:#faf7f2;color:#392f2a;padding:24px;border:1px solid #c5b7ad;border-radius:16px}
  .piano-dialog::backdrop{background:#0006}.piano-dialog header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
  .piano-dialog h2{font-size:22px;margin:0}.piano-dialog label{display:block;margin:12px 0}.piano-dialog input,.piano-dialog select{background:white;color:#392f2a;border:1px solid #c5b7ad;padding:8px;border-radius:6px;max-width:100%}
  .piano-dialog button,.piano-dialog a{padding:8px 12px;background:#eee3d7;color:#392f2a;border-radius:8px;border:1px solid #c5b7ad;cursor:pointer}.piano-dialog button:disabled{opacity:.5;cursor:wait}
  .piano-dialog article{padding:16px 0;border-top:1px solid #dfd4c9}.piano-dialog audio{display:block;width:100%;margin:12px 0}.piano-dialog p{margin:10px 0}`;
  document.head.append(css);
  async function request(path,body){
    const response=await fetch(new URL(path,base),{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
    const value=await response.json();if(!response.ok||value.code&&value.code!==200)throw Error(t('暂时没能完成，请稍后再试'));return value.data;
  }
  function node(tag,text,parent){const el=document.createElement(tag);if(text)el.textContent=text;if(parent)parent.append(el);return el}
  function close(){if(timer)clearInterval(timer);timer=null;if(dialog){dialog.querySelectorAll('audio').forEach(a=>a.pause());dialog.remove();dialog=null}}
  async function open(){
    if(!caps||dialog)return;dialog=node('dialog');dialog.className='piano-dialog';
    const head=node('header',null,dialog);node('h2',t('钢琴小样'),head);const dismiss=node('button',t('关闭'),head);dismiss.onclick=close;
    node('p',t('上传你的 MIDI，录成一段可以播放和下载的 MP3。'),dialog);
    const prices=caps.price_ranges_cents;
    node('p',t('MIDI 小样 ¥{min}–{max}',{min:(prices.midi[0]/100).toFixed(2),max:(prices.midi[1]/100).toFixed(2)})+(caps.original?t('；原创小样 ¥{min}–{max}',{min:(prices.original[0]/100).toFixed(2),max:(prices.original[1]/100).toFixed(2)}):''),dialog);
    const collection=node('p','',dialog);collection.setAttribute('aria-label',t('TA 给我的曲子'));
    const form=node('form',null,dialog),fileLabel=node('label',t('MIDI 文件（.mid / .midi）'),form),file=node('input',null,fileLabel);file.type='file';file.accept='.mid,.midi';file.required=true;
    const titleLabel=node('label',t('曲名'),form),title=node('input',null,titleLabel);title.maxLength=60;title.required=true;
    const durationLabel=node('label',t('录音时长（秒）'),form),duration=node('input',null,durationLabel);duration.type='number';duration.min='30';duration.max=String(caps.max_seconds);duration.value=String(Math.min(60,caps.max_seconds));duration.required=true;
    const sectionLabel=node('label',t('演奏段落'),form),section=node('select',null,sectionLabel);
    for(const [text,label] of [['开头',t('开头')],['中段',t('中段')],['结尾',t('结尾')]]){const option=node('option',label,section);option.value=text}
    file.onchange=()=>{if(file.files[0])title.value=file.files[0].name.replace(/\.midi?$/i,'').slice(0,60)};
    const submit=node('button',t('生成 MP3'),form);submit.type='submit';const status=node('p','',dialog);status.setAttribute('role','status');const list=node('section',null,dialog);list.setAttribute('aria-label',t('钢琴任务列表'));
    async function load(){
      if(!dialog||busy)return;busy=true;
      try{const latest=await request('/toy/midi/capabilities');collection.textContent=t('TA 给我的曲子：{pieces}',{pieces:(latest.pieces||[]).map(p=>t('《{title}》',{title:p.title})).join(t('、'))||t('还没有上传')});
        const data=await request('/toy/midi/listJobs?pageSize=100');
        const signature=JSON.stringify(data.list);if(list.dataset.signature===signature)return;list.dataset.signature=signature;
        list.querySelectorAll('audio').forEach(a=>a.pause());list.replaceChildren();
        for(const job of data.list){const row=node('article',null,list);node('strong',job.piano_meta?.title||job.filename,row);
          node('p',({1:t('排队中'),2:t('正在录制'),3:t('已完成'),4:t('已取消'),5:t('未能完成')})[job.state]||t('等待更新'),row);
          if(job.description)node('p',job.description,row);
          if(job.state===3&&job.info?.audioUrl){const audio=node('audio',null,row);audio.controls=true;audio.preload='metadata';audio.src=job.info.audioUrl;
            const link=node('a',t('下载 MP3'),row);link.href=job.info.audioUrl+'?download=1';link.download='piano.mp3';}
          const action=node('button',[1,2].includes(job.state)?t('取消'):t('移除记录'),row);
          action.onclick=async()=>{action.disabled=true;try{await request('/toy/midi/'+([1,2].includes(job.state)?'cancelGenerate':'deleteJob'),{jobId:job.jobId});await load()}catch(e){status.textContent=e.message;action.disabled=false}};
        }
      }catch(e){status.textContent=e.message}finally{busy=false}
    }
    let pending=null;
    form.onsubmit=async event=>{event.preventDefault();const selected=file.files[0];if(!selected)return;
      if(!/\.midi?$/i.test(selected.name)){status.textContent=t('请选择 .mid 或 .midi 文件');return}
      if(selected.size>caps.max_midi_bytes){status.textContent=t('这份 MIDI 太大了，请换一份较小的文件');return}
      const header=new Uint8Array(await selected.slice(0,4).arrayBuffer());if(String.fromCharCode(...header)!=='MThd'){status.textContent=t('这份文件不是有效的 MIDI');return}
      if([...title.value].length>60){status.textContent=t('曲名请控制在 60 字以内');return}
      if(submit.disabled)return;submit.disabled=true;status.textContent=t('正在上传 MIDI…');
      try{if(!pending){const ticket=await request('/toy/genObjectUploadUrl',{filename:selected.name,type:'midi'});
          const uploaded=await fetch(ticket.url,{method:'PUT',headers:ticket.headers,body:selected});if(!uploaded.ok)throw Error(t('MIDI 上传失败，请检查文件后再试'));
          pending={midiUrl:ticket.key,title:title.value.trim(),seconds:Number(duration.value),section:section.value};
          file.disabled=title.disabled=duration.disabled=section.disabled=true;}
        await request('/toy/midi/generate',pending);pending=null;file.disabled=title.disabled=duration.disabled=section.disabled=false;status.textContent=t('收到 MIDI 了，正在准备录音。');await load();
      }catch(e){status.textContent=e.message}finally{submit.disabled=false}
    };
    dialog.addEventListener('cancel',e=>{e.preventDefault();close()});document.body.append(dialog);dialog.showModal();await load();timer=setInterval(load,4000);
  }
  function mount(){document.querySelectorAll(original).forEach(card=>{let button=card.previousElementSibling;if(!button?.classList.contains('piano-entry')){button=node('button',t('钢琴小样 · 上传 MIDI / 查看录音'));button.className='piano-entry';button.type='button';button.onclick=open;card.before(button)}button.hidden=!caps})}
  const observer=new MutationObserver(mount);observer.observe(document.documentElement,{childList:true,subtree:true});mount();
  async function capability(){try{const value=await request('/toy/midi/capabilities');caps=value.enabled===true&&value.price_ranges_cents?.midi&&value.price_ranges_cents?.original?value:null}catch{caps=null}if(!caps)close();mount()}
  capability();setInterval(capability,60000);
})();
'''
