from pathlib import Path
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def music_limits_document():
    component = BOOTSTRAP_JAVASCRIPT[BOOTSTRAP_JAVASCRIPT.index('  const mountMusicSettings ='):BOOTSTRAP_JAVASCRIPT.index('  const mountDiagnosticExport =')]
    return '''<!doctype html><meta charset="utf-8"><style>
body{margin:32px;background:#141516;color:#edece9;font:15px/1.6 "Segoe UI",sans-serif}main{max-width:640px;margin:auto}button{padding:9px 16px;border:1px solid #8886;border-radius:9px;background:#242528;color:inherit;cursor:pointer}button:disabled{opacity:.5}small,p{color:#b9bdc6}details{border:1px solid #8884;padding:16px;border-radius:12px}h3{margin:0}label{margin-top:12px}
</style><main id="settings"></main><pre id="acceptance"></pre><script>
const native=new URL(location.href).searchParams.get('provider')==='native';
const text=(tag,copy,cls)=>{const el=document.createElement(tag);el.textContent=copy;el.className=cls||'';return el};
const button=(copy,fn)=>{const el=text('button',copy);el.type='button';el.addEventListener('click',fn);return el};
const actions=()=>{const el=document.createElement('div');el.style.cssText='display:flex;gap:10px;flex-wrap:wrap';return el};
const SETUP_STATUS_PATH='/toy/setup/status';let setupSessionToken='synthetic',saved=null;
const defaults={caption:'',title:'',duration:null,negative_tags:'',style_weight:null,weirdness_constraint:null};
const requestSetup=async(path,body)=>{if(body.action==='music_settings_save'){saved=body.options;return {status:'OK'}};return {defaults,options:{...defaults,caption:native?'x'.repeat(140):'saved legacy jazz',duration:210},original_music_provider:'suno_v6',...(native?{original_music_input_limits:{lyrics:3000,style:120,duration_control:false}}:{})}};
''' + component + '''
const settings=mountMusicSettings(document.getElementById('settings'));
const check=(ok,message)=>{if(!ok)throw Error(message)};
(async()=>{await Promise.resolve();await Promise.resolve();
const caption=document.querySelector('[name=caption]'),duration=document.querySelector('[name=duration]');
if(native){
check(caption.maxLength===120,'native style bound');check(caption.value.length===140,'saved style retained');
check(duration.disabled&&duration.value==='210','unsupported duration retained and disabled');
check(document.querySelector('[role=status]').textContent.includes('120'),'saved conflict visible');
let rejected=false;try{settings.read()}catch(e){rejected=true};check(rejected,'conflicts cannot submit');
const automatic=[...document.querySelectorAll('button')].find(el=>el.textContent==='改为服务自动时长');check(automatic&&!automatic.hidden,'explicit automatic action');
document.querySelector('details').open=true;
if(!new URL(location.href).searchParams.has('screenshot')){
automatic.click();check(duration.value==='','explicit action cleared duration');caption.value='user piano jazz';
const value=settings.read();check(value.caption==='user piano jazz'&&value.duration===null,'user style retained without forced duration');
const save=[...document.querySelectorAll('button')].find(el=>el.textContent==='保存音乐设置');save.click();await Promise.resolve();await Promise.resolve();
check(saved?.caption==='user piano jazz'&&saved?.duration===null,'valid options saved');check(duration.disabled,'disabled persists after busy state');
}
}else{check(caption.maxLength===1000&&!duration.disabled,'legacy controls unchanged');const value=settings.read();check(value.duration===210&&value.caption==='saved legacy jazz','legacy settings retained')}
document.getElementById('acceptance').textContent='PASS';
})().catch(e=>document.getElementById('acceptance').textContent='FAIL:'+e.message);
</script>'''


@pytest.mark.parametrize('provider', ['legacy', 'native'])
def test_music_limits_preserve_settings_and_require_explicit_auto_duration(tmp_path, provider):
    browser = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not browser.is_file():
        pytest.skip('Headless Chrome unavailable')
    page = tmp_path / 'music-limits.html'
    page.write_text(music_limits_document(), encoding='utf8')
    result = subprocess.run([str(browser),'--headless','--disable-gpu','--no-first-run',
        '--no-default-browser-check','--disable-background-networking','--disable-component-update',
        '--disable-extensions',f'--user-data-dir={tmp_path / "profile"}','--virtual-time-budget=1000',
        '--dump-dom',page.as_uri() + '?provider=' + provider],capture_output=True,text=True,
        encoding='utf8',errors='replace',timeout=45)
    assert result.returncode == 0
    assert '<pre id="acceptance">PASS</pre>' in result.stdout,result.stdout.split('<pre id="acceptance">')[-1][:300]
