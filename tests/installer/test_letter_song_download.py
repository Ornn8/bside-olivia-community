import shutil
import subprocess

import pytest

from patch_companion_settings import _repair_native_letter_audio


def test_two_audio_downloads_wait_for_both_native_tasks(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    # Frozen original-client store function, exercised after patching.
    source = '''async function X(B,K){J.value=0,Q.value=!1,H.value=!0;const W=await Qm({sourceUrls:[B],destPath:K}),ue=Object.values(W.data)[0];he.value=ue,xe=setInterval(async()=>{try{const{data:re}=await e1([he.value]),ye=re[0];if(!ye)return;ye.totalBytes>0&&(J.value=Math.round(ye.downloadedBytes/ye.totalBytes*100)),ye.state===zo.Completed?($e(),J.value=100,Q.value=!0,Ds(K)):(ye.state===zo.Failed||ye.state===zo.Cancelled)&&($e(),H.value=!1,wt.alert({message:e("share_download_error")}))}catch(re){$e(),H.value=!1}},1e3)}async function te(){$e(),he.value&&await t1([he.value]),H.value=!1,J.value=0,Q.value=!1}'''
    patched = _repair_native_letter_audio(source)
    harness = '''const assert=require('node:assert/strict');
const J={},Q={},H={},he={},zo={Completed:1,Failed:2,Cancelled:3};let xe,poll,finished=false,opened=0,cancelled;
const setInterval=f=>(poll=f,1),$e=()=>{},Ds=()=>opened++,wt={alert(){throw Error('unexpected failure')}},e=x=>x;
const Qm=async args=>{assert.deepEqual(args.sourceUrls,['speech.wav','song.wav']);return {data:{a:'speech',b:'song'}}};
const e1=async ids=>{assert.deepEqual(ids,['speech','song']);return {data:[{totalBytes:10,downloadedBytes:10,state:1},{totalBytes:10,downloadedBytes:finished?10:0,state:finished?1:0}]}};
const t1=async ids=>{cancelled=ids};
'''+patched+'''
(async()=>{await X(['speech.wav','song.wav'],'destination');await poll();assert.equal(J.value,50);assert.equal(Q.value,false);assert.equal(opened,0);
finished=true;await poll();assert.equal(Q.value,true);assert.equal(opened,1);await te();assert.deepEqual(cancelled,['speech','song']);})().catch(e=>{console.error(e);process.exitCode=1});'''
    script=tmp_path/'download.cjs'
    script.write_text(harness,encoding='utf8')
    subprocess.run([node,str(script)],check=True,capture_output=True)
