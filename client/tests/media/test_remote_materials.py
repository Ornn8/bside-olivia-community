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


@pytest.mark.parametrize('include_spoken', [False, True])
def test_cloud_music_without_local_model_assets(tmp_path, monkeypatch, include_spoken):
    from pathlib import Path
    from runtime.remote_generation import RemoteGeneration
    from runtime.media.remote_materials import render_music_materials
    submitted = []
    async def request(self, action, data):
        if action == 'capabilities':
            return {'kinds': ['original_video'], 'shared_assets': [
                {'asset_id': name, 'sha256': '0' * 64} for name in (
                    'official-performance-lipsync-safe-2950f-v1', 'official-reply-action-base-v1')]}
        if action == 'ack':
            assert (tmp_path / 'result.mp4').read_bytes() == b'assembled'
            return {'status': 'acknowledged'}
        assert action == 'submit'
        submitted.append(data['input'])
        return {'task_id': 'synthetic-task', 'status': 'succeeded', 'outputs': []}
    async def download(self, task, output, **kwargs):
        payload = b'material'
        names = ['song.wav', 'song.mp4'] + (['speech.mp4'] if include_spoken else [])
        files = {n: {'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)} for n in names}
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('manifest.json', json.dumps({'version': 1, 'assembly': 'client', 'files': files}))
            for name in names: archive.writestr(name, payload)
        return task
    monkeypatch.setattr(RemoteGeneration, 'request', request)
    monkeypatch.setattr(RemoteGeneration, '_download', download)
    monkeypatch.setattr('runtime.media.latentsync_reply.resolve_ffmpeg_executable', lambda _: Path('ffmpeg'))
    monkeypatch.setattr('runtime.media.music_reply._media_duration_seconds', lambda *a, **k: 10)
    monkeypatch.setattr('runtime.media.music_reply._run', lambda args, *a, **k: Path(args[-1]).write_bytes(b'assembled'))
    def concat(speech, song, output, **kwargs):
        assert kwargs['transition_video_path'] is None
        output.write_bytes(b'assembled')
    monkeypatch.setattr('runtime.media.music_reply.concat_videos', concat)
    # The real local-server caller represents unavailable scenes with Path().
    result = render_music_materials('original_video', {'lyrics': 'synthetic'}, tmp_path / 'result.mp4',
        environment={}, include_spoken=include_spoken, reply_text='synthetic',
        performance_video_path=Path(), official_reply_reference_path=Path(), spoken_action_base_path=None)
    assert result['assembly'] == 'local'
    assert submitted[0]['scene_asset'] == 'official-performance-lipsync-safe-2950f-v1'
    if include_spoken:
        assert submitted[0]['spoken_scene_asset'] == 'official-reply-action-base-v1'
