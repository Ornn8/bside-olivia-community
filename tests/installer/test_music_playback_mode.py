import shutil
import subprocess
from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT

from runtime.personal_chat._patch_music_playback import patch_music_playback


def test_native_music_modes_and_library_end(tmp_path):
    # A small native-store fixture: keep the production anchors and execute the
    # patched next/end handlers, rather than asserting that labels were added.
    source = '''
const p={value:'repeat'},ot={Repeat:'repeat',Shuffle:'shuffle',Single:'single'};
const h={value:'songlist'},f={value:{id:'a'}},u={value:null},I={value:[]};
const t={value:'lite'},Se={LITE:'lite',PRO:'pro'},m={value:true},d={value:0};
const x={value:[]},a=()=>true,$=()=>-1,M=()=>{},w=()=>{};
const sent=[],played=[],Ct=v=>sent.push(v),i={isDownloaded:id=>id!=='missing'};
const A=s=>{f.value=s;played.push(s.id)};
const _e=(ref,fn)=>fn();
const ne=()=>{p.value===ot.Shuffle?p.value=ot.Repeat:p.value===ot.Repeat?p.value=ot.Single:p.value===ot.Single&&(p.value=ot.Shuffle,u.value?I.value=[u.value.itemId]:I.value=[]),t.value===Se.LITE&&Ct({cmd:"setPlayMode",mode:p.value})};
const U=()=>{if(h.value==="songlist"){const K=x.value.findIndex(W=>a(W));K!==-1&&M(x.value[K]);return}const B=$();B!==-1&&x.value[B]&&M(x.value[B])};
const S=()=>{if(h.value==="songlist")return;};
const O=B=>{switch(B.event){case"ended":if(m.value=!1,w("natural_end"),h.value==="songlist"){const K=x.value.findIndex(W=>a(W));K!==-1?M(x.value[K]):(f.value=null,d.value=0)}else p.value===ot.Single&&u.value&&a(u.value)?M(u.value):U();break;}};
function store(){return{isSongAvailable:a,isPlaying:ao(m)}}
const ao=v=>v;
'''
    patched = patch_music_playback(source)
    assert patched != source
    assert patch_music_playback(patched) == patched
    script = tmp_path / 'music.js'
    script.write_text('''
const assert=require('node:assert/strict');
global.window=new EventTarget();
window.__oliviaLocalSongCatalog={songs:{value:[{id:'a'},{id:'missing'},{id:'b'},{id:'c'}]}};
''' + patched + '''
store();const mode=window.__oliviaMusicPlayback;
assert.equal(mode.mode,'repeat');
O({event:'ended'});assert.equal(played.at(-1),'b');
U();assert.equal(played.at(-1),'c');
U();assert.equal(played.at(-1),'a');
S();assert.equal(played.at(-1),'c');
mode.setMode('shuffle');assert.equal(mode.mode,'shuffle');
assert.equal(sent.at(-1).mode,'shuffle');
const count=played.length;mode.setMode('repeat');assert.equal(played.length,count);
mode.setMode('shuffle');Math.random=()=>0;U();assert.equal(played.at(-1),'a');
U();assert.equal(played.at(-1),'b');
ne();assert.equal(mode.mode,'repeat');ne();assert.equal(mode.mode,'shuffle');
mode.setMode('invalid');assert.equal(mode.mode,'shuffle');
window.__oliviaLocalSongCatalog.songs.value=[{id:'b'}];U();assert.equal(played.at(-1),'b');
window.__oliviaLocalSongCatalog.songs.value=[];O({event:'ended'});assert.equal(f.value,null);
''', encoding='utf-8')
    result = subprocess.run([shutil.which('node') or 'node', str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_music_selector_clicks_and_route_lifecycle(tmp_path):
    browser = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not browser.is_file():
        pytest.skip('Headless Chromium unavailable')
    source = BOOTSTRAP_JAVASCRIPT
    ui = source[source.index('  const syncMusicMode ='):source.index('  const isSettingsRoute =')]
    page = tmp_path / 'music.html'
    page.write_text('''<!doctype html><meta charset="utf-8">
<nav data-olivia-main-navigation></nav><pre id="result"></pre><script>
const button=(label,fn)=>{const b=document.createElement('button');b.textContent=label;b.onclick=fn;return b};
const openLocalSongs=()=>{};
''' + ui + '''
const check=(ok,label)=>{if(!ok)throw Error(label)};
try {
location.hash='#/studio';mountLocalSongEntry();
let select=document.querySelector('select');check(select.disabled,'wait for native store');
window.__oliviaMusicPlayback={mode:'repeat',setMode(mode){this.mode=mode}};
window.dispatchEvent(new Event('olivia-music-mode-changed'));
check(!select.disabled&&select.value==='repeat','default sequential');
check([...select.options].map(o=>o.textContent).join(',')==='顺序播放,随机播放','labels');
select.value='shuffle';select.dispatchEvent(new Event('change'));
check(window.__oliviaMusicPlayback.mode==='shuffle','native mode changed');
mountLocalSongEntry();check(document.querySelectorAll('select').length===1,'no duplicate');
location.hash='#/collection';mountLocalSongEntry();check(!document.querySelector('select'),'remove');
location.hash='#/studio';mountLocalSongEntry();check(document.querySelector('select').value==='shuffle','retain native state');
document.getElementById('result').textContent='PASS';
}catch(e){document.getElementById('result').textContent='FAIL:'+e.message}
</script>''', encoding='utf-8')
    result = subprocess.run([str(browser), '--headless', '--disable-gpu', '--no-first-run',
                             '--disable-background-networking', '--disable-component-update',
                             f'--user-data-dir={tmp_path / "profile"}', '--virtual-time-budget=1000',
                             '--dump-dom', page.as_uri()], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=60)
    assert '<pre id="result">PASS</pre>' in result.stdout, result.stdout[-2000:]
