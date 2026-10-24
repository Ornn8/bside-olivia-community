from pathlib import Path
import os
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT

SHIM = Path(__file__).resolve().parents[1].joinpath("js_shim", "olivia_i18n_shim.js").read_text(encoding="utf-8")


def composer_document():
    source=BOOTSTRAP_JAVASCRIPT
    composer='const proactiveState={busy:false};\n'+source[source.index('  const composerCovers ='):source.index('  const mountVideoReplySetting')]
    composer+=source[source.index('  const mountMusicSettings ='):source.index('  const mountDiagnosticExport =')]
    wave=source[source.index('  function letterWave('):source.index('  const coverApi=')]
    return (Path(__file__).parents[1]/'fixtures/cover_composer.html').read_text(encoding='utf-8').replace('/* COMPOSER */',composer).replace('/* WAVE */',wave).replace('<script>','<script data-shim>'+SHIM+'</script><script>',1)


@pytest.mark.parametrize('asr,width,provider', [('ready',1200,'suno'),('missing',700,'suno'),('failed',1200,'suno'),('ready',1200,'legacy')])
def test_inline_cover_composer_preserves_drafts_and_submission(tmp_path,asr,width,provider):
    browser=shutil.which('google-chrome') or shutil.which('chromium')
    if os.name=='nt':
        browser=next((str(p) for p in [Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')] if p.is_file()),None)
    if not browser:pytest.skip('Headless Chromium unavailable')
    page=tmp_path/'composer.html';page.write_text(composer_document(),encoding='utf-8')
    result=subprocess.run([browser,'--headless','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-extensions',f'--user-data-dir={tmp_path / "profile"}',f'--window-size={width},960','--virtual-time-budget=1500','--dump-dom',page.as_uri()+f'?verify&asr={asr}&provider={provider}'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
    assert result.returncode==0,result.stderr[-1500:]
    assert '<pre id="acceptance">PASS</pre>' in result.stdout,result.stdout.split('<pre id="acceptance">', 1)[-1][:500]


@pytest.mark.parametrize('mode', ['cover', 'original'])
@pytest.mark.parametrize('phase', ['preview', 'quote'])
def test_music_output_changed_during_preflight_requires_new_confirmation(tmp_path, mode, phase):
    browser = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not browser.is_file():
        pytest.skip('Headless Chromium unavailable')
    document = composer_document().replace('const routeRequest=', 'const fixtureRouteRequest=').replace(
        'const requestSetup=', 'const fixtureRequestSetup=')
    document = document.replace('<script>', '''<script>
let releasePreview,holdPreview=true;
const phase=new URLSearchParams(location.search).get('phase');
const routeRequest=async(...args)=>{
 if(holdPreview&&phase==='preview')await new Promise(resolve=>releasePreview=resolve);
 return fixtureRouteRequest(...args);
};
const requestSetup=async(path,body)=>{
 if(holdPreview&&phase==='quote'&&body?.action==='billing_quote')await new Promise(resolve=>releasePreview=resolve);
 return fixtureRequestSetup(path,body);
};
''')
    document = document.replace('</script></html>', '''
(async()=>{
 const check=(ok,message)=>{if(!ok)throw Error(message)};
 const mode=new URLSearchParams(location.search).get('mode'),state=composerCovers.get(input);
 state.select(mode);if(mode==='cover')await testFile();else await new Promise(resolve=>setTimeout(resolve,0));
 paid=true;approvePayment=true;
 const config={url:'/toy/letter/send',data:{content:input.value}};
 const pending=window.__oliviaPrepareLetterRoute(config);
 await new Promise(resolve=>setTimeout(resolve,0));
 document.querySelector('.olivia-compose-'+mode+' .olivia-cover-options button:last-child').click();
 releasePreview();
 let canceled=false;try{await pending}catch(error){canceled=error.__CANCEL__===true}
 check(canceled,'stale audio submission accepted after video selected');
 check(!config.data.material,'canceled submission mutated the request');
 check(state.materialForSend()[mode==='cover'?'cover_output':'original_output']==='video','video selection lost');
 holdPreview=false;
 const prepared=await window.__oliviaPrepareLetterRoute({url:'/toy/letter/send',data:{content:input.value}});
 check(prepared.data.material[mode==='cover'?'cover_output':'original_output']==='video','resend did not use video');
 check(paymentMessage.includes('视频回信')&&paymentMessage.includes('¥5.00'),'video confirmation missing format or price');
 document.querySelector('.olivia-compose-'+mode+' .olivia-cover-options button').click();
 await window.__oliviaPrepareLetterRoute({url:'/toy/letter/send',data:{content:input.value}});
 check(paymentMessage.includes('音频回信')&&paymentMessage.includes('¥1.00'),'audio confirmation missing format or price');
 document.getElementById('acceptance').textContent='PASS';
})().catch(error=>document.getElementById('acceptance').textContent='FAIL:'+error.message);
</script></html>''')
    page = tmp_path / 'selection.html'
    page.write_text(document, encoding='utf-8')
    result = subprocess.run([str(browser), '--headless', '--disable-gpu', '--no-first-run',
        '--disable-background-networking', '--disable-component-update',
        f'--user-data-dir={tmp_path / "profile"}', '--virtual-time-budget=1500',
        '--dump-dom', page.as_uri() + '?mode=' + mode + '&phase=' + phase], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=45)
    assert result.returncode == 0, result.stderr[-1500:]
    assert '<pre id="acceptance">PASS</pre>' in result.stdout, result.stdout.split('<pre id="acceptance">')[-1][:500]


def test_letter_pictures_upload_send_with_an_ordinary_letter_and_leave_once_sent(tmp_path):
    browser = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not browser.is_file():
        pytest.skip('Headless Chromium unavailable')
    document = composer_document().replace('</script></html>', '''
const fixtureFetch=window.fetch;let uploads=0,holdUpload=false,releaseUpload=null;
const pictureId=n=>String(n).padStart(32,'0');
window.fetch=async(url,request)=>{
 if(new URL(url).pathname!=='/toy/letter/image/upload')return fixtureFetch(url,request);
 if(holdUpload)await new Promise(resolve=>releaseUpload=resolve);
 const n=++uploads;
 return {ok:true,json:async()=>({code:0,data:{image_id:pictureId(n),url:'http://127.0.0.1:8899/toy/media/letter-image-'+pictureId(n)+'.png'}})};
};
(async()=>{
 const check=(ok,message)=>{if(!ok)throw Error(message)};
 const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 const state=composerCovers.get(input),strip=document.querySelector('.olivia-compose-pictures');
 const pictureInput=document.querySelector('.olivia-compose-grid>input[type=file]');
 const choose=async(...names)=>{const dt=new DataTransfer();for(const name of names)dt.items.add(new File([new Uint8Array(8)],name,{type:'image/png'}));pictureInput.files=dt.files;await pictureInput.onchange()};
 const prepare=()=>window.__oliviaPrepareLetterRoute({url:'/toy/letter/send',data:{content:input.value}});
 const add=[...strip.querySelectorAll('button')].find(node=>node.textContent==='添加图片');
 check(add&&!strip.hidden&&strip.closest('.olivia-compose-navigation'),'picture button beside the letter tabs');
 check(document.querySelector('input[type=file]').accept.includes('.wav'),'the song file stays the first file input');
 await choose('a.png','b.JPG','notes.txt');
 check(state.images.length===2&&strip.querySelectorAll('.olivia-compose-picture img').length===2,'two pictures, text refused');
 check(strip.textContent.includes('PNG'),'type hint shown');
 let config=await prepare();
 check(JSON.stringify(config.data.material.image_ids)===JSON.stringify([pictureId(1),pictureId(2)]),'pictures named in the letter');
 check(!('cover_source_id' in config.data.material)&&config.data.content==='今晚想听你唱一首歌。','ordinary letter otherwise');
 check(!('cover_source_id' in calls.filter(item=>item.path==='/toy/letter/route-preview').pop().body),'preview asks no cover');
 strip.querySelector('.olivia-compose-picture button').click();
 check(state.images.length===1&&state.images[0].image_id===pictureId(2),'one removed');
 await choose('c.webp','d.png','e.jpeg','f.png');
 check(state.images.length===4&&add.hidden&&strip.textContent.includes('4'),'at most four');
 state.select('cover');check(strip.hidden,'song pages hide the pictures');
 await testFile();config=await prepare();check(!config.data.material.image_ids&&config.data.material.cover_source_id,'a cover carries no pictures');
 state.select('letter');check(!strip.hidden&&state.images.length===4,'pictures kept across tabs');
 state.clearPictures();holdUpload=true;const pending=choose('g.png');await wait(0);
 check(add.disabled,'busy while uploading');
 let blocked='';try{await prepare()}catch(error){blocked=error.message}
 check(blocked.includes('图片还在添加'),'no send while a picture uploads');
 releaseUpload();await pending;holdUpload=false;
 config=await prepare();check(config.data.material.image_ids.length===1,'uploaded picture sent');
 await wait(600);check(state.images.length===1,'kept while the send is under way');
 input.value='';await wait(600);check(state.images.length===0,'gone once the dialog empties its draft');
 input.value='另一封信';await choose('h.png');document.getElementById('clear').click();
 check(state.images.length===0,'clear empties the pictures too');
 document.getElementById('acceptance').textContent='PASS';
})().catch(error=>document.getElementById('acceptance').textContent='FAIL:'+error.message);
</script></html>''')
    page = tmp_path / 'pictures.html'
    page.write_text(document, encoding='utf-8')
    result = subprocess.run([str(browser), '--headless', '--disable-gpu', '--no-first-run',
        '--disable-background-networking', '--disable-component-update',
        f'--user-data-dir={tmp_path / "profile"}', '--virtual-time-budget=5000',
        '--dump-dom', page.as_uri()], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=45)
    assert result.returncode == 0, result.stderr[-1500:]
    assert '<pre id="acceptance">PASS</pre>' in result.stdout, result.stdout.split('<pre id="acceptance">')[-1][:500]


def test_audio_collection_reuses_existing_waveform_and_stays_outside_paper():
    source = BOOTSTRAP_JAVASCRIPT
    assert "collect.className='olivia-wave-style'" in source
    assert "toolbar.append(collect)" in source
    assert "if(paper)paper.before(toolbar);else this.before(toolbar)" in source
    assert "this.waveCleanup=letterWave(wave,seek,audio,url)" in source
    assert "'/toy/local-songs/from-letter'" in source
    assert "cancelAnimationFrame(placementFrame);toolbar.remove()" in source


def test_world_main_navigation_lifecycle(tmp_path):
    browser=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not browser.is_file():pytest.skip('Headless Chromium unavailable')
    source=BOOTSTRAP_JAVASCRIPT
    nav=source[source.index('  const WORLD_ROUTE ='):source.index('  const finishInitialSetup =')]
    page=tmp_path/'world.html'
    page.write_text('''<!doctype html><meta charset="utf-8"><pre id="acceptance"></pre><script>'''+SHIM+'''</script><script>
const text=(tag,copy)=>{const n=document.createElement(tag);n.textContent=copy;return n};
const button=(copy,fn)=>{const n=text('button',copy);n.onclick=fn;return n};
const STATUS_PATH='/status';let reads=0;
const requestJson=async()=>{reads++;return {capabilities:{private_world:{state:'available'}}}};
const renderPrivateWorldPanel=async(panel)=>panel.append(text('p','真实接口内容'));
const openDialog=()=>{};
let routeRecord,pushes=[];
window.__oliviaNativeView={h:(tag)=>document.createElement(tag),router:{hasRoute:()=>!!routeRecord,addRoute:r=>routeRecord=r,push:path=>pushes.push(path)}};
''' + nav + '''
(async()=>{const check=(ok,msg)=>{if(!ok)throw Error(msg)};
installNativeWorldRoute();check(routeRecord.path==='/world','independent native route');
location.hash=WORLD_ROUTE;mountMainNavigation();
const view=routeRecord.component,el=view.render();document.body.append(el);view.mounted.call({$el:el});await Promise.resolve();
const links=[...document.querySelectorAll('nav a')];check(links.map(n=>n.textContent).join(',')==='信箱,世界,物品栏,曲库','navigation order');
const navigationLeft=document.querySelector('nav').style.left;
check(links[1].getAttribute('aria-current')==='page','world selected');check(document.querySelector('main [data-world-main]'),'world main mounted');
mountMainNavigation();check(reads===1,'does not reload on DOM changes');
links[3].click();check(pushes[0]==='/studio','navigation uses native router');
view.beforeUnmount.call({$el:el});el.remove();location.hash='#/studio';mountMainNavigation();check(!document.querySelector('[data-olivia-world-page]'),'world removed');check(links[3].getAttribute('aria-current')==='page','studio selected');check(document.querySelector('nav').style.left===navigationLeft,'navigation stays in place');
location.hash='#/collection';mountMainNavigation();check(links[0].getAttribute('aria-current')==='page','mailbox selected');
document.getElementById('acceptance').textContent='PASS';
})().catch(e=>document.getElementById('acceptance').textContent='FAIL:'+e.message);
</script>''',encoding='utf-8')
    result=subprocess.run([str(browser),'--headless','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update',f'--user-data-dir={tmp_path / "profile"}','--virtual-time-budget=1000','--dump-dom',page.as_uri()],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
    assert '<pre id="acceptance">PASS</pre>' in result.stdout,result.stdout[-1800:]


def test_letter_pictures_can_be_dragged_onto_an_ordinary_letter(tmp_path):
    browser = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not browser.is_file():
        pytest.skip('Headless Chromium unavailable')
    document = composer_document().replace('</script></html>', '''
const fixtureFetch=window.fetch;let uploads=0;
window.fetch=async(url,request)=>{
 if(new URL(url).pathname!=='/toy/letter/image/upload')return fixtureFetch(url,request);
 const n=String(++uploads).padStart(32,'0');
 return {ok:true,json:async()=>({code:0,data:{image_id:n,url:'http://127.0.0.1:8899/toy/media/letter-image-'+n+'.png'}})};
};
(async()=>{
 const check=(ok,message)=>{if(!ok)throw Error(message)};
 const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 const state=composerCovers.get(input),grid=document.querySelector('.olivia-compose-grid');
 const typeOf=name=>({png:'image/png',webp:'image/webp',txt:'text/plain'})[name.split('.').pop()];
 const files=(...names)=>{const dt=new DataTransfer();for(const name of names)dt.items.add(new File([new Uint8Array(8)],name,{type:typeOf(name)}));return dt};
 const drag=(type,dt)=>{const event=new DragEvent(type,{bubbles:true,cancelable:true,dataTransfer:dt});grid.dispatchEvent(event);return event};
 // The host page may stop drag events on the way down; the composer hears them first.
 for(const type of ['dragenter','dragover','drop'])document.addEventListener(type,event=>event.stopPropagation(),true);
 let dt=files('a.png','b.webp','notes.txt');
 check(drag('dragover',dt).defaultPrevented&&grid.classList.contains('olivia-compose-dropping'),'files over a letter are taken');
 check(drag('drop',dt).defaultPrevented&&!grid.classList.contains('olivia-compose-dropping'),'drop accepted');
 await wait(50);
 check(state.images.length===2,'two pictures added, text refused');
 state.select('cover');dt=files('c.png');
 check(!drag('dragover',dt).defaultPrevented&&!drag('drop',dt).defaultPrevented,'song pages ignore dropped pictures');
 await wait(50);state.select('letter');check(state.images.length===2,'nothing added on a song page');
 // A copied picture often has no file name; pasting it adds it, and text pasted with it still goes in.
 const paste=dt=>{const event=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:dt});input.dispatchEvent(event);return event};
 dt=new DataTransfer();dt.items.add(new File([new Uint8Array(8)],'',{type:'image/png'}));
 check(paste(dt).defaultPrevented,'a pasted picture alone is taken');
 await wait(50);check(state.images.length===3,'pasted picture added');
 dt=new DataTransfer();dt.items.add(new File([new Uint8Array(8)],'',{type:'image/jpeg'}));dt.setData('text/plain','看这个');
 check(!paste(dt).defaultPrevented,'text pasted with a picture still goes in');
 await wait(50);check(state.images.length===4,'picture pasted with text added');
 document.getElementById('acceptance').textContent='PASS';
})().catch(error=>document.getElementById('acceptance').textContent='FAIL:'+error.message);
</script></html>''')
    page = tmp_path / 'drop.html'
    page.write_text(document, encoding='utf-8')
    result = subprocess.run([str(browser), '--headless', '--disable-gpu', '--no-first-run',
        '--disable-background-networking', '--disable-component-update',
        f'--user-data-dir={tmp_path / "profile"}', '--virtual-time-budget=5000',
        '--dump-dom', page.as_uri()], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=45)
    assert result.returncode == 0, result.stderr[-1500:]
    assert '<pre id="acceptance">PASS</pre>' in result.stdout, result.stdout.split('<pre id="acceptance">')[-1][:500]
