import asyncio
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from runtime import image_reply, image_understanding, remote_generation
from runtime.cloud_service import CloudError


PLAN = dict(attach=True, photo_type='snapshot', room='music-workstation', time_of_day='night', prompt='Coffee on a wooden desk')


def test_missing_pillow_fails_before_paid_submission(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == 'PIL':
            raise ModuleNotFoundError('synthetic')
        return original(name, *args, **kwargs)
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch)
        monkeypatch.setattr(builtins, '__import__', guarded)
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert row['image_error_code'] == 'IMAGE_DEPENDENCY_MISSING'
        assert row['image_phase'] == 'dependency'
        assert calls['submits'] == []
    asyncio.run(scenario())


def setup(tmp_path, monkeypatch, failure='download'):
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'http://127.0.0.1:9')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic-only')
    calls = {'submits': [], 'downloads': 0, 'plans': []}
    async def tools(**kwargs):
        calls['plans'].append(kwargs['request_id'])
        return [SimpleNamespace(name='plan_reply_photo', arguments=dict(PLAN))]
    class Remote(remote_generation.RemoteGeneration):
        async def request(self, action, data):
            if action == 'capabilities': return {'kinds': ['image'], 'shared_assets': []}
            assert action == 'submit'
            calls['submits'].append(data['request_id'])
            if failure == 'balance': raise CloudError('GPU_INSUFFICIENT_BALANCE', 402)
            if failure == 'terminal': return dict(task_id='saved-task', status='failed', outputs=[])
            return dict(task_id='saved-task', status='succeeded', outputs=[{'url': 'http://invalid.example/photo.png'}])
        async def _download(self, task, output, *, validate=None):
            calls['downloads'] += 1
            if task['status'] == 'failed': raise CloudError('GPU_TASK_FAILED', 502)
            if failure in {'download', 'always'} and (failure == 'always' or calls['downloads'] == 1):
                raise CloudError('GPU_CONNECTION_FAILED')
            if failure == 'cancel' and calls['downloads'] == 1: raise asyncio.CancelledError()
            Image.new('RGB', (768, 1024), 'gray').save(output)
            validate(output)
            return task
    monkeypatch.setattr(remote_generation, 'RemoteGeneration', Remote)
    async def describe(*a, **kw):
        if failure == 'vision': raise TimeoutError('synthetic')
        return {'source': 'generated', 'summary': 'A coffee mug'}
    monkeypatch.setattr(image_understanding, 'describe_image', describe)
    server = SimpleNamespace(video_reply_settings_store=SimpleNamespace(image_snapshot=lambda: {'enabled': True, 'resolution': '1K'}),
        letters_adapter=SimpleNamespace(gateway=SimpleNamespace(complete_with_tools=tools)),
        _persist_store_state=lambda: None, _state_root=lambda: tmp_path, media_semaphore=asyncio.Semaphore(1), PORT=1234)
    row = {'letter_id': 'reply-one'}
    return server, row, calls


def test_transient_result_download_recovers_same_paid_request(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch)
        await image_reply._prepare_once(server, row, 'coffee?', 'Here')
        assert row['image_status'] == 'RETRY_PENDING' and row['image_receipt_required'] is True
        assert row['image_phase'] == 'download' and row['image_cloud_status'] == 'succeeded'
        row['image_retry_at'] = 0
        await image_reply.prepare(server, row, 'coffee?', 'Here')
        assert row['image_status'] == 'COMPLETED'
        assert row['image_phase'] == 'ready' and row['image_dependency_available'] is True
        assert len(calls['submits']) == 2 and len(set(calls['submits'])) == 1
        assert len(calls['plans']) == 1 and calls['plans'][0].startswith('reply-photo-plan-')
        assert row['image_description']['source'] == 'generated'
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['balance', 'terminal'])
def test_server_terminal_failure_never_creates_auto_retry(tmp_path, monkeypatch, failure):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, failure)
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'FAILED'
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert len(calls['submits']) == 1
    asyncio.run(scenario())


def test_retry_budget_caps_network_failures_without_new_gpu_jobs(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, 'always')
        for attempt in range(6):
            row['image_retry_at'] = 0
            await image_reply._prepare_once(server, row, 'photo', 'Okay')
        assert len(calls['submits']) == 4 and len(set(calls['submits'])) == 1
        assert row['image_generation_failures'] == 4 and row['image_status'] == 'FAILED'
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['remove', 'corrupt', 'empty_submission', 'new_credentials'])
def test_damaged_or_rebound_receipt_cannot_trigger_fresh_charge(tmp_path, monkeypatch, damage):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch)
        await image_reply._prepare_once(server, row, 'photo', 'Okay')
        receipt = next((tmp_path / 'media').glob('*.task.json'))
        if damage == 'remove': receipt.unlink()
        elif damage == 'corrupt': receipt.write_text('bad')
        elif damage == 'empty_submission':
            saved = json.loads(receipt.read_text()); saved['submission'] = {}
            receipt.write_text(json.dumps(saved))
        else: monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'different-synthetic-key')
        row['image_retry_at'] = 0
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert len(calls['submits']) == 1 and row['image_status'] == 'FAILED'
    asyncio.run(scenario())


def test_cancelled_attempt_resumes_receipt_not_new_job(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, 'cancel')
        with pytest.raises(asyncio.CancelledError):
            await image_reply.prepare(server, row, 'photo', 'Okay')
        assert row['image_receipt_required']
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'COMPLETED' and len(set(calls['submits'])) == 1
    asyncio.run(scenario())


def test_vision_failure_preserves_successfully_paid_photo(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, 'vision')
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'COMPLETED' and row['image_description_status'] == 'PENDING'
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert len(calls['submits']) == 1
    asyncio.run(scenario())


def test_fresh_world_location_blocks_conflicting_photo_before_gpu_charge(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch)
        server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(snapshot=lambda now: {
            'stale': False, 'current': {'location': '家中厨房'}}))
        await image_reply.prepare(server, row, '发张现在的照片', '我在厨房。')
        assert row['image_status'] == 'SKIPPED' and row['image_skip_reason'] == 'SCENE_CONFLICT'
        assert calls['plans'] and calls['submits'] == []
        assert 'image_plan' not in row
    asyncio.run(scenario())


def test_scene_location_matching_uses_catalog_ids_and_home_boundary():
    assert image_reply._scene_matches_location('music-workstation', 'music_room')
    assert image_reply._scene_matches_location('record_shop', '唱片店')
    assert not image_reply._scene_matches_location('cafe', '家中厨房')
    assert not image_reply._scene_matches_location('kitchen', 'record_shop')


def test_photo_planner_ignores_historical_life_location_after_new_reply():
    store = SimpleNamespace(
        reply_context=lambda query, *, now: json.dumps({'stale': True, 'current': None}),
        snapshot=lambda now: {'stale': False, 'current': {'location': '家中厨房'}})
    server = SimpleNamespace(daily_life_runtime=SimpleNamespace(store=store))
    assert image_reply._current_location(server) is None


def test_cancel_after_local_result_recovers_without_remote_submit(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, 'vision')
        seen = []
        async def describe(*args, **kwargs):
            seen.append(1)
            if len(seen) == 1: raise asyncio.CancelledError()
            return {'source': 'generated', 'summary': 'Coffee mug'}
        monkeypatch.setattr(image_understanding, 'describe_image', describe)
        with pytest.raises(asyncio.CancelledError):
            await image_reply.prepare(server, row, 'photo', 'Okay')
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'COMPLETED' and len(calls['submits']) == 1
    asyncio.run(scenario())
