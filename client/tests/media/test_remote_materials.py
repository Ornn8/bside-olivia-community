import hashlib
import json
import zipfile

import pytest

from runtime.cloud_service import CloudError
from runtime.media.remote_materials import extract_materials


@pytest.mark.parametrize('corruption', [None, 'hash', 'traversal', 'duplicate', 'missing'])
def test_material_package_validation(tmp_path, corruption):
    path = tmp_path / 'materials.zip'
    payload = b'synthetic'
    files = {name: {'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)} for name in ('song.wav', 'song.mp4')}
    if corruption == 'hash': files['song.wav']['sha256'] = '0' * 64
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({'version': 1, 'assembly': 'client', 'files': files}))
        archive.writestr('song.wav', payload)
        if corruption != 'missing': archive.writestr('song.mp4', payload)
        if corruption == 'traversal': archive.writestr('../escape', payload)
        if corruption == 'duplicate':
            with pytest.warns(UserWarning): archive.writestr('song.wav', payload)
    destination = tmp_path / 'materials'
    destination.mkdir()
    if corruption:
        with pytest.raises(CloudError): extract_materials(path, destination, include_spoken=False)
    else:
        assert extract_materials(path, destination, include_spoken=False) == {'song.wav', 'song.mp4'}
        assert (destination / 'song.wav').read_bytes() == payload
    assert not (tmp_path / 'escape').exists()


@pytest.mark.parametrize('fail_assembly', [False, True])
def test_ack_only_after_final_local_assembly(tmp_path, monkeypatch, fail_assembly):
    from pathlib import Path
    from runtime.media.remote_materials import render_music_materials
    final = tmp_path / 'result.mp4'
    calls = []
    class API:
        url='https://gpu.example'
        token='synthetic-key'
        def __init__(self,*args):pass
        async def generate(self, kind, data, output, **kwargs):
            payload=b'material'
            files={n:{'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)} for n in ('song.wav','song.mp4')}
            with zipfile.ZipFile(output,'w') as z:
                z.writestr('manifest.json',json.dumps({'version':1,'assembly':'client','files':files}))
                for n in files:z.writestr(n,payload)
            return {'task_id':'task-1234'}
        async def request(self, action, data):
            assert final.read_bytes()==b'assembled'
            calls.append(action)
            return {'task_id':data['task_id'],'status':'acknowledged'}
    monkeypatch.setattr('runtime.remote_generation.RemoteGeneration',API)
    monkeypatch.setattr('runtime.media.latentsync_reply.resolve_ffmpeg_executable',lambda _:Path('ffmpeg'))
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds',lambda *a,**k:10)
    def mux(args,*a,**k):
        if fail_assembly:raise RuntimeError('mux failed')
        Path(args[-1]).write_bytes(b'assembled')
    monkeypatch.setattr('runtime.media.music_reply._run',mux)
    def execute():
        return render_music_materials('original_video',{'lyrics':'synthetic'},final,
            environment={},include_spoken=False,reply_text='',performance_video_path=tmp_path/'scene.mp4',
            official_reply_reference_path=tmp_path/'transition.mp4')
    if fail_assembly:
        with pytest.raises(RuntimeError,match='mux failed'):execute()
        assert not final.exists() and calls==[]
    else:
        assert execute()['assembly']=='local'
        assert calls==['ack']
