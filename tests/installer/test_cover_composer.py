from pathlib import Path
import os
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def composer_document():
    source=BOOTSTRAP_JAVASCRIPT
    composer='const proactiveState={busy:false};\n'+source[source.index('  const composerCovers ='):source.index('  const mountVideoReplySetting')]
    wave=source[source.index('  function letterWave('):source.index('  const coverApi=')]
    return (Path(__file__).parents[1]/'fixtures/cover_composer.html').read_text(encoding='utf-8').replace('/* COMPOSER */',composer).replace('/* WAVE */',wave)


@pytest.mark.parametrize('asr,width', [('ready',1200),('missing',700),('failed',1200)])
def test_inline_cover_composer_preserves_drafts_and_submission(tmp_path,asr,width):
    browser=shutil.which('google-chrome') or shutil.which('chromium')
    if os.name=='nt':
        browser=next((str(p) for p in [Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')] if p.is_file()),None)
    if not browser:pytest.skip('Headless Chromium unavailable')
    page=tmp_path/'composer.html';page.write_text(composer_document(),encoding='utf-8')
    result=subprocess.run([browser,'--headless','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-extensions',f'--user-data-dir={tmp_path / "profile"}',f'--window-size={width},960','--virtual-time-budget=1500','--dump-dom',page.as_uri()+f'?verify&asr={asr}'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
    assert result.returncode==0,result.stderr[-1500:]
    assert '<pre id="acceptance">PASS</pre>' in result.stdout,result.stdout[-1800:]


def test_audio_collection_reuses_existing_waveform_and_stays_outside_paper():
    source = BOOTSTRAP_JAVASCRIPT
    assert "collect.className='olivia-wave-style'" in source
    assert "document.body.append(collect)" in source
    assert "this.waveCleanup=letterWave(wave,seek,audio,url)" in source
    assert "'/toy/local-songs/from-letter'" in source
    assert "collect.remove();styles.remove();" in source


def test_world_main_navigation_lifecycle(tmp_path):
    browser=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not browser.is_file():pytest.skip('Headless Chromium unavailable')
    source=BOOTSTRAP_JAVASCRIPT
    nav=source[source.index('  const WORLD_ROUTE ='):source.index('  const finishInitialSetup =')]
    page=tmp_path/'world.html'
    page.write_text('''<!doctype html><meta charset="utf-8"><pre id="acceptance"></pre><script>
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
const links=[...document.querySelectorAll('nav a')];check(links.map(n=>n.textContent).join(',')==='信箱,世界,曲库','navigation order');
const navigationLeft=document.querySelector('nav').style.left;
check(links[1].getAttribute('aria-current')==='page','world selected');check(document.querySelector('main [data-world-main]'),'world main mounted');
mountMainNavigation();check(reads===1,'does not reload on DOM changes');
links[2].click();check(pushes[0]==='/studio','navigation uses native router');
view.beforeUnmount.call({$el:el});el.remove();location.hash='#/studio';mountMainNavigation();check(!document.querySelector('[data-olivia-world-page]'),'world removed');check(links[2].getAttribute('aria-current')==='page','studio selected');check(document.querySelector('nav').style.left===navigationLeft,'navigation stays in place');
location.hash='#/collection';mountMainNavigation();check(links[0].getAttribute('aria-current')==='page','mailbox selected');
document.getElementById('acceptance').textContent='PASS';
})().catch(e=>document.getElementById('acceptance').textContent='FAIL:'+e.message);
</script>''',encoding='utf-8')
    result=subprocess.run([str(browser),'--headless','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update',f'--user-data-dir={tmp_path / "profile"}','--virtual-time-budget=1000','--dump-dom',page.as_uri()],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
    assert '<pre id="acceptance">PASS</pre>' in result.stdout,result.stdout[-1800:]
