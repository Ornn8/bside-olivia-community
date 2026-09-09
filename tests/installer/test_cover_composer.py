import json
import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


@pytest.mark.parametrize('asr', ['ready', 'missing', 'failed'])
def test_cover_upload_recognizes_before_manual_confirmation(tmp_path, asr):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    source = BOOTSTRAP_JAVASCRIPT
    source = source[source.index('  const composerCovers ='):source.index('  window.__oliviaPrepareLetterRoute')]
    script = tmp_path / 'composer.cjs'
    script.write_text('const asr=' + json.dumps(asr) + ';\n' + r'''
const assert=require('node:assert/strict');
const nodes=[],calls=[];
const make=tag=>{const n={tag,style:{},value:'',files:[],children:[],append(...children){this.children.push(...children)},setAttribute(k,v){this[k]=v},addEventListener(){},showModal(){},close(){},remove(){},focus(){},pause(){}};nodes.push(n);return n};
const document={createElement:make,activeElement:null,body:make('body')};
const text=(tag,copy)=>{const n=make(tag);n.textContent=copy;return n};
const button=(copy,fn)=>{const n=text('button',copy);n.onclick=fn;return n};
const actions=()=>make('div');const apiBase='http://127.0.0.1:8899';
const CONFIRM_HEADER='X-Olivia-Companion-Action',CONFIRM_VALUE='confirmed';
URL.createObjectURL=()=> 'blob:mock';URL.revokeObjectURL=()=>{};
const fetch=async(url,request)=>{calls.push({path:new URL(url).pathname,request});
 if(new URL(url).pathname==='/toy/cover/upload')return {ok:true,json:async()=>({code:0,data:{source_id:'a'.repeat(32),asr_available:asr!=='missing'}})};
 if(new URL(url).pathname==='/toy/cover/lyrics')return {ok:asr!=='failed',json:async()=>asr==='failed'?{code:400,data:{}}:{code:0,data:{lyrics:'识别结果',language:'zh'}}};
 throw Error('Unexpected generation or send');
};
''' + source + r'''
(async()=>{
 const pending=selectCoverAudio();
 const file=nodes.find(n=>n.type==='file');file.files=[{name:'原曲.mp3',size:100}];await file.onchange();
 const lyrics=nodes.find(n=>n.tag==='textarea');
 assert.equal(lyrics.value,asr==='ready'?'识别结果':'');
 assert.equal(calls.filter(x=>x.path==='/toy/cover/lyrics').length,asr==='missing'?0:1);
 assert.equal(calls[0].request.headers[CONFIRM_HEADER],'confirmed');
 lyrics.value='人工修正歌词';
 const submit=nodes.find(n=>n.textContent==='使用这首原曲');assert.equal(submit.disabled,false);submit.onclick();
 const material=await pending;
 assert.equal(material.cover_source_id,'a'.repeat(32));assert.equal(material.cover_lyrics,'人工修正歌词');
 assert.equal(material.cover_output,'audio');assert.equal(material.filename,'原曲.mp3');
 assert(!calls.some(x=>x.path.includes('send')));
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_audio_collection_reuses_existing_waveform_and_stays_outside_paper():
    source = BOOTSTRAP_JAVASCRIPT
    assert "collect.className='olivia-wave-style'" in source
    assert "document.body.append(collect)" in source
    assert "this.waveCleanup=letterWave(wave,seek,audio,url)" in source
    assert "'/toy/local-songs/from-letter'" in source
    assert "collect.remove();styles.remove();" in source
