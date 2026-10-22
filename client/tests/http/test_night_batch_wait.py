"""Orders the service runs only in its night batch are waited for, resumed after a restart, and shown as queued for
tonight (2026-10-22: a cover asked for by day was cancelled after an hour and the letter said it failed)."""
import asyncio
import json

import pytest

from runtime.cloud_service import CloudError

NIGHT = {'kinds': ['cover', 'lipsync', 'original', 'separate'], 'start': '03:00', 'end': '07:00',
         'timezone': 'Asia/Shanghai'}


def fake_service(statuses, *, caps=None):
    """A service whose task stays queued for the night for the given polls, then succeeds."""
    calls, polls = [], list(statuses)

    async def request(action, data):
        calls.append(action)
        if action == 'capabilities':
            return {'kinds': ['cover', 'tts'], 'shared_assets': [], **(caps or {})}
        if action == 'submit':
            return {'task_id': 'task-night-1', 'status': 'queued', 'stage': 'queued',
                    'night_hold': {'starts_at': 1792699200}}
        if action == 'status':
            return polls.pop(0) if polls else {'task_id': 'task-night-1', 'status': 'succeeded', 'stage': 'completed'}
        if action == 'cancel':
            return {'task_id': data['task_id'], 'status': 'cancelled'}
        raise AssertionError(action)
    return request, calls


def held(n):
    return [{'task_id': 'task-night-1', 'status': 'queued', 'stage': 'queued',
             'night_hold': {'starts_at': 1792699200}}] * n


def run(api, kind, output, **kwargs):
    async def download(task, path, *, validate=None, attempts=3):
        path.write_bytes(b'song')
        return task
    api._download = download
    return asyncio.run(api.generate(kind, {'lyrics': 'la'}, output, **kwargs))


def test_a_night_kind_is_waited_for_past_the_old_limit_and_never_cancelled(tmp_path, monkeypatch):
    from runtime import remote_generation
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(remote_generation.asyncio, 'sleep', sleep)
    api = remote_generation.RemoteGeneration('https://gpu.example', 'synthetic-key')
    api.request, calls = fake_service(held(5), caps={'night_batch': NIGHT})
    task = run(api, 'cover', tmp_path / 'song.wav', timeout=0)
    assert task['status'] == 'succeeded' and 'cancel' not in calls
    assert slept[:5] == [remote_generation.NIGHT_POLL_SECONDS] * 5   # held polls are slow ones


def test_without_the_night_batch_the_old_limit_still_applies(tmp_path, monkeypatch):
    from runtime import remote_generation

    async def sleep(seconds):
        pass
    monkeypatch.setattr(remote_generation.asyncio, 'sleep', sleep)
    api = remote_generation.RemoteGeneration('https://gpu.example', 'synthetic-key')
    api.request, calls = fake_service(held(5))
    with pytest.raises(CloudError) as raised:
        run(api, 'cover', tmp_path / 'song.wav', timeout=0)
    assert raised.value.code == 'GPU_TASK_TIMEOUT' and calls[-1] == 'cancel'


def test_the_pipeline_keeps_a_receipt_and_a_night_marker_while_held(tmp_path, monkeypatch):
    from runtime import remote_generation, remote_pipeline
    output = tmp_path / 'media' / 'letter-1-cover.wav'
    output.parent.mkdir()
    seen = []

    async def sleep(seconds):
        marker = output.with_name('letter-1-cover-night.json')
        seen.append(json.loads(marker.read_text()) if marker.exists() else None)
    monkeypatch.setattr(remote_generation.asyncio, 'sleep', sleep)
    request, calls = fake_service(held(2), caps={'night_batch': NIGHT})
    monkeypatch.setattr(remote_generation.RemoteGeneration, 'request', lambda self, action, data: request(action, data))

    async def download(self, task, path, *, validate=None, attempts=3):
        path.write_bytes(b'song')
        return task
    monkeypatch.setattr(remote_generation.RemoteGeneration, '_download', download)

    async def upload(self, path):
        return 'asset-1'
    monkeypatch.setattr(remote_generation.RemoteGeneration, 'upload', upload)
    source = tmp_path / 'source.wav'
    source.write_bytes(b'source')
    result = remote_pipeline.generate('cover', {'lyrics': 'la'}, output, assets={'source_asset': source},
                                      environment={'OLIVIA_GPU_API_URL': 'https://gpu.example',
                                                   'OLIVIA_GPU_API_KEY': 'synthetic-key'})
    assert result['remote_task_id'] == 'task-night-1'
    assert seen[0] == {'starts_at': 1792699200}                     # the letter can say it waits for tonight
    assert not output.with_name('letter-1-cover-night.json').exists()  # gone once the order moved on
    receipt = json.loads(output.with_name('letter-1-cover-remote-order.private.json').read_text())
    assert receipt['task_id'] == 'task-night-1' and receipt['submission']['kind'] == 'cover'


def test_a_saved_night_order_survives_a_restart(tmp_path, monkeypatch):
    import local_server as server
    monkeypatch.setattr(server, '_local_data_root', lambda *args: tmp_path)
    stages = tmp_path / 'media' / 'letter-2-music-v2-180s-stages'
    stages.mkdir(parents=True)
    letter = {'letter_id': 'letter-2'}
    assert server._has_saved_night_order(letter) is False
    order = stages / 'song.partial-remote-order.private.json'
    order.write_text(json.dumps({'fingerprint': 'f' * 64, 'task_id': 'task-9',
                                 'submission': {'request_id': 'req-123456789', 'kind': 'original', 'input': {}}}))
    assert server._has_saved_night_order(letter) is True
    order.write_text(json.dumps({'fingerprint': 'f' * 64,                      # submitted but never answered
                                 'submission': {'request_id': 'req-123456789', 'kind': 'original', 'input': {}}}))
    assert server._has_saved_night_order(letter) is False
