"""Exercise real envelope drawing and lifecycle without loading the app or GPU."""
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_waveform_media_read_allows_only_trusted_origin(tmp_path, monkeypatch):
    import asyncio
    from aiohttp.test_utils import make_mocked_request
    import local_server

    (tmp_path / 'sample.wav').write_bytes(b'RIFF')
    monkeypatch.setattr(local_server, '_media_root', lambda: tmp_path)
    monkeypatch.setattr(local_server, 'origin_allowed', lambda origin: origin == 'http://127.0.0.1:8876')
    for origin in ('http://127.0.0.1:8876', 'https://untrusted.example'):
        response = asyncio.run(local_server._media_handler(make_mocked_request(
            'GET', '/toy/media/sample.wav', headers={'Origin': origin})))
        assert response.headers.get('Access-Control-Allow-Origin') == (
            origin if origin == 'http://127.0.0.1:8876' else None)


def test_waveform_tracks_audio_and_releases_resources(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    source = BOOTSTRAP_JAVASCRIPT.split('  function letterWave(', 1)[1].split('  window.__oliviaLetterWave=', 1)[0]
    harness = r'''
const assert=require('node:assert/strict');
let lines=[],color='',closed=0,disconnected=0,frames=new Map(),next=0,level=128,reads=0;
const ctx={setTransform(){},clearRect(){lines=[]},beginPath(){},moveTo(x,y){this.y=y},lineTo(x,y){lines.push({height:y-this.y,color})},stroke(){},set strokeStyle(v){color=v}};
const canvas={width:0,height:0,setAttribute(){},getContext(){return ctx}};
global.document={hidden:false,createElement(){return canvas},addEventListener(){},removeEventListener(){}};
global.window={devicePixelRatio:1,AudioContext:class{
  state='suspended';async resume(){}
  createAnalyser(){return {connect(){},disconnect(){},getByteTimeDomainData(data){reads++;for(let i=0;i<data.length;i++)data[i]=i%2?128:level}}}
  createMediaElementSource(){return {connect(){},disconnect(){}}}
  async close(){this.state='closed';closed++}
}};
global.matchMedia=()=>({matches:false});
global.ResizeObserver=class{observe(){}disconnect(){disconnected++}};
global.requestAnimationFrame=f=>{frames.set(++next,f);return next};
global.cancelAnimationFrame=id=>frames.delete(id);
global.fetch=async(url,{signal})=>{
  signal.addEventListener('abort',()=>aborted=true);let done=false;
  return {ok:true,headers:{get(){return '4'}},body:{getReader(){
    return {async read(){if(done)return {done:true};done=true;return {done:false,value:new Uint8Array(4)}}};
  }}};
};
const listeners={};const audio={paused:true,ended:false,duration:8,currentTime:0,addEventListener(n,f){listeners[n]=f},removeEventListener(n){delete listeners[n]}};
const wrap={clientWidth:400,append(){}};
const dispose=letterWave(wrap,{},audio,'http://127.0.0.1/toy/media/test.wav');
setImmediate(()=>{
  assert.equal(closed,0);
  assert.equal(frames.size,0,'paused waveform should not animate');
  dispose.start();audio.currentTime=6;audio.paused=false;listeners.play();
  assert.equal(frames.size,1);assert.equal(reads,1);const silent=JSON.stringify(lines);
  level=190;listeners.timeupdate();assert.notEqual(JSON.stringify(lines),silent,'live audio changes waveform');
  const pictures=new Set();for(const style of ['bars','dots','ribbon','ripple']){dispose.setStyle(style);pictures.add(JSON.stringify(lines));assert.equal(audio.currentTime,6);assert.equal(audio.paused,false)}
  assert.equal(pictures.size,4,'four distinct selectable shapes');
  const before=reads;audio.paused=true;listeners.pause();assert.equal(frames.size,0);assert.equal(reads,before);
  dispose();assert.equal(closed,1);assert.equal(disconnected,1);assert.equal(Object.keys(listeners).length,0);
});
'''
    script = tmp_path / 'wave.cjs'
    script.write_text('function letterWave(' + source + harness, encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
