import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from PIL import Image

from runtime import image_reply, image_understanding, remote_generation
from runtime.cloud_service import CloudError


PLAN = dict(attach=True, photo_type='snapshot', room='music-workstation', time_of_day='night', prompt='Coffee on a wooden desk')


@pytest.mark.parametrize('server_planned', [False, True])
def test_selected_image_model_is_frozen_and_resumed_after_catalog_removal(tmp_path, monkeypatch, server_planned):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch)
        row['image_reply_settings'] = {'enabled': True, 'resolution': '1K', 'model': 'approved-photo'}
        base = remote_generation.RemoteGeneration
        caps = {'kinds': ['image'], 'shared_assets': [], 'server_media_planning': server_planned,
                'image': {'models': [{'id': 'approved-photo', 'display_name': 'Photo', 'resolutions': ['1K']}]}}
        class Cloud(base):
            async def request(self, action, data):
                if action == 'capabilities': return caps
                result = await super().request(action, data)
                if server_planned: result['media_plan'] = {k: v for k, v in PLAN.items() if k != 'attach'}
                return result
        monkeypatch.setattr(remote_generation, 'RemoteGeneration', Cloud)
        await image_reply._prepare_once(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'RETRY_PENDING'
        assert calls['submissions'][0]['input']['model'] == 'approved-photo'
        caps['image']['models'] = []
        server.video_reply_settings_store.image_snapshot = lambda: {'enabled': True, 'resolution': '1K', 'model': 'new-model'}
        row['image_retry_at'] = 0
        await image_reply._prepare_once(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'COMPLETED'
        assert len(calls['submits']) == 1 and calls['statuses'] == ['saved-task']
        assert row['image_reply_settings']['model'] == 'approved-photo'
    asyncio.run(scenario())


def test_unadvertised_image_model_fails_before_any_paid_or_planning_request(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch)
        row['image_reply_settings'] = {'enabled': True, 'resolution': '1K', 'model': 'local-only-photo'}
        await image_reply._prepare_once(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'FAILED' and row['image_error_code'] == 'IMAGE_MODEL_UNAVAILABLE'
        assert not calls['submits'] and not calls['plans']
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['model', 'remove', 'corrupt'])
def test_server_planned_model_binding_cannot_create_a_second_paid_request(tmp_path, monkeypatch, damage):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch)
        row['image_reply_settings'] = {'enabled': True, 'resolution': '1K', 'model': 'approved-photo'}
        base = remote_generation.RemoteGeneration
        class Cloud(base):
            async def request(self, action, data):
                if action == 'capabilities': return {'kinds': ['image'], 'shared_assets': [], 'server_media_planning': True,
                    'image': {'models': [{'id': 'approved-photo', 'display_name': 'Photo', 'resolutions': ['1K']}]}}
                result = await super().request(action, data)
                result['media_plan'] = {k: v for k, v in PLAN.items() if k != 'attach'}
                return result
        monkeypatch.setattr(remote_generation, 'RemoteGeneration', Cloud)
        await image_reply._prepare_once(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'RETRY_PENDING'
        receipt = next((tmp_path / 'media').glob('*.task.json'))
        if damage == 'model': row['image_reply_settings']['model'] = 'different-photo'
        elif damage == 'remove': receipt.unlink()
        else: receipt.write_text('bad')
        row['image_retry_at'] = 0
        await image_reply._prepare_once(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'FAILED'
        assert row['image_error_code'] in {'IMAGE_GENERATION_BINDING_CHANGED', 'IMAGE_RECEIPT_MISSING', 'IMAGE_RECEIPT_INVALID'}
        assert len(calls['submits']) == 1 and not calls['statuses']
    asyncio.run(scenario())


def test_completed_photo_survives_state_save_failure_without_new_charge(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, failure='none')
        saves = []
        def persist():
            if row.get('image_phase') == 'ready':
                saves.append(row['image_status'])
                if len(saves) == 1:
                    raise OSError('synthetic state write failure')
        server._persist_store_state = persist
        with pytest.raises(OSError):
            await image_reply.prepare(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'COMPLETED'
        assert Path(row['prepared_image']).is_file()
        assert 'image_error_code' not in row
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert saves == ['COMPLETED', 'COMPLETED']
        assert len(calls['submits']) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['timeout', 'busy', 'unavailable'])
def test_transient_photo_planner_failure_recovers_before_gpu_submission(tmp_path, monkeypatch, kind):
    from llm_gateway import ProviderTimeout, ProviderRetryableError, ProviderUnavailable
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, failure='none')
        original = server.letters_adapter.gateway.complete_with_tools
        attempts = []
        async def flaky(**kwargs):
            attempts.append(kwargs['request_id'])
            if len(attempts) == 1:
                raise {'timeout': ProviderTimeout(), 'busy': ProviderRetryableError(503), 'unavailable': ProviderUnavailable()}[kind]
            return await original(**kwargs)
        server.letters_adapter.gateway.complete_with_tools = flaky
        await image_reply._prepare_once(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'RETRY_PENDING'
        assert calls['submits'] == []
        row['image_retry_at'] = 0
        await image_reply.prepare(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'COMPLETED'
        assert len(calls['submits']) == 1 and len(set(attempts)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('code', ['PROVIDER_AUTH_FAILED', 'PROVIDER_QUOTA_EXHAUSTED', 'PROVIDER_USAGE_PENDING'])
def test_terminal_planner_failure_is_not_retried(tmp_path, monkeypatch, code):
    from llm_gateway import GatewayError
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, failure='none')
        async def rejected(**kwargs):
            raise GatewayError(code, retryable=False)
        server.letters_adapter.gateway.complete_with_tools = rejected
        await image_reply._prepare_once(server, row, 'photo', 'Okay')
        assert row['image_status'] == 'FAILED' and row['image_error_code'] == code
        assert calls['submits'] == []
    asyncio.run(scenario())


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
    calls = {'submits': [], 'statuses': [], 'downloads': 0, 'plans': [], 'submissions': []}
    async def tools(**kwargs):
        calls['plans'].append(kwargs['request_id'])
        return [SimpleNamespace(name='plan_reply_photo', arguments=dict(PLAN))]
    class Remote(remote_generation.RemoteGeneration):
        async def request(self, action, data):
            if action == 'capabilities': return {'kinds': ['image'], 'shared_assets': []}
            if action == 'status':
                assert data == {'task_id': 'saved-task'}
                calls['statuses'].append(data['task_id'])
                return dict(task_id='saved-task', status='succeeded', outputs=[{'url': 'http://invalid.example/photo.png'}])
            assert action == 'submit'
            calls['submits'].append(data['request_id'])
            calls['submissions'].append(data)
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


def test_qq_photo_submits_while_other_media_holds_slot(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, failure=None)
        await server.media_semaphore.acquire()
        await asyncio.wait_for(image_reply.prepare(server, row, 'photo', 'Here', channel='qq'), 2)
        assert row['image_status'] == 'COMPLETED'
        assert len(calls['submits']) == 1
        assert server.media_semaphore.locked()
    asyncio.run(scenario())


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
        assert len(calls['submits']) == 1 and calls['statuses'] == ['saved-task']
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
        assert len(calls['submits']) == 1 and calls['statuses'] == ['saved-task'] * 3
        assert calls['downloads'] == 4
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
        assert row['image_status'] == 'COMPLETED' and len(calls['submits']) == 1
        assert calls['statuses'] == ['saved-task']
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
        bind_photo_view(row, location='家中厨房', text='我在厨房。')
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


PHOTO_TIME = datetime(2026, 9, 27, 3, tzinfo=timezone.utc)


def bind_photo_view(row, *, location='music_room', text='给你看看桌上的咖啡。', world=True, emotion=None):
    from runtime.reply.character_emotion_context import freeze_expression_context, store_expression_context
    row.update(reply_text=text, reply_revision=1)
    view = {'kind': 'character_life_reference', 'stale': False,
            'current': {'location': location, 'evidence_kind': 'published_life',
                        'source_id': 'PRIVATE_WORLD_SOURCE', 'note': 'PRIVATE_WORLD_NOTE'}} if world else None
    snapshot = freeze_expression_context('synthetic-photo-reply', PHOTO_TIME, world=view, emotion=emotion)
    store_expression_context(row, snapshot, text)


def test_delayed_photo_and_planning_retry_use_reply_view_without_private_state(tmp_path, monkeypatch):
    from llm_gateway import ProviderTimeout
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, failure='none')
        live_reads = []
        server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(snapshot=lambda now:
            live_reads.append(now) or {'stale': False, 'current': {'location': '家中厨房'}}))
        bind_photo_view(row, emotion={'reaction_subject': 'character', 'interpretation_only': True,
            'reactions': [{'reaction': 'relieved', 'source_id': 'PRIVATE_EMOTION_SOURCE',
                          'quote': 'PRIVATE_QUOTE', 'goal_or_need': 'PRIVATE_GOAL'}],
            'concerns': [{'summary': 'PRIVATE_CONCERN'}],
            'reported_affects': [{'subject': 'user', 'quote': 'PRIVATE_USER_AFFECT'}]})
        planned = []
        async def plan(**kwargs):
            planned.append(kwargs)
            if len(planned) == 1:
                raise ProviderTimeout()
            return [SimpleNamespace(name='plan_reply_photo', arguments=dict(PLAN))]
        server.letters_adapter.gateway.complete_with_tools = plan
        await image_reply._prepare_once(server, row, '发张随手拍', row['reply_text'])
        assert row['image_status'] == 'RETRY_PENDING'
        row['image_retry_at'] = 0
        await image_reply.prepare(server, row, '发张随手拍', row['reply_text'])
        packets = [json.loads(call['messages'][-1]['content']) for call in planned]
        assert [p['world_current_location'] for p in packets] == ['music_room', 'music_room']
        assert [p['reply_as_of'] for p in packets] == [PHOTO_TIME.isoformat()] * 2
        assert all(p['expression_options'] == ['relieved'] for p in packets)
        assert live_reads == [] and row['image_status'] == 'COMPLETED'
        assert 'PRIVATE_' not in json.dumps(planned + calls['submissions'])
        payload = calls['submissions'][0]['input']
        assert set(payload) == {'photo_type', 'room', 'time_of_day', 'prompt', 'resolution'}
        assert payload['photo_type'] == 'snapshot' and payload['prompt'] == PLAN['prompt']
    asyncio.run(scenario())


@pytest.mark.parametrize('missing', ['legacy', 'corrupt', 'omitted', 'stale', 'statement', 'body', 'revision', 'argument'])
def test_unavailable_photo_view_never_fills_from_live_world(tmp_path, monkeypatch, missing):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, failure='none')
        bind_photo_view(row, world=missing != 'omitted')
        if missing == 'legacy':
            row.pop('expression_context')
        elif missing == 'corrupt':
            row['expression_context']['view_sha256'] = 'damaged'
        elif missing in {'stale', 'statement'}:
            from runtime.reply.character_emotion_context import freeze_expression_context, store_expression_context
            world = row['expression_context']['world']
            if missing == 'stale':
                world['stale'] = True
            else:
                world['current']['evidence_kind'] = 'character_statement'
            store_expression_context(row, freeze_expression_context('synthetic', PHOTO_TIME, world=world), row['reply_text'])
        elif missing == 'body':
            row['reply_text'] += '换一条正文'
        elif missing == 'revision':
            row['reply_revision'] += 1
        live_reads = []
        server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(snapshot=lambda now:
            live_reads.append(now) or {'stale': False, 'current': {'location': '家中厨房'}}))
        packets = []
        async def plan(**kwargs):
            packets.append(json.loads(kwargs['messages'][-1]['content']))
            return [SimpleNamespace(name='plan_reply_photo', arguments=dict(PLAN))]
        server.letters_adapter.gateway.complete_with_tools = plan
        text = '另一条正文' if missing == 'argument' else row['reply_text']
        await image_reply.prepare(server, row, '发张随手拍', text)
        assert packets[0]['world_current_location'] is None and live_reads == []
        assert row['image_status'] == 'COMPLETED'
        assert calls['submissions'][0]['input']['room'] == 'none'
    asyncio.run(scenario())


@pytest.mark.parametrize('emotion', [
    {'reaction_subject': 'character', 'interpretation_only': True, 'pending_current_input': True,
     'reactions': [{'reaction': 'hurt'}]},
    {'reaction_subject': 'character', 'interpretation_only': True, 'reactions': None},
    {'reaction_subject': 'character', 'interpretation_only': True, 'reactions': [{'reaction': {'bad': 1}}]},
])
def test_photo_ignores_pending_or_malformed_emotion_without_blocking(tmp_path, monkeypatch, emotion):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, failure='none')
        bind_photo_view(row, emotion=emotion)
        packets = []
        async def plan(**kwargs):
            packets.append(json.loads(kwargs['messages'][-1]['content']))
            return [SimpleNamespace(name='plan_reply_photo', arguments=dict(PLAN))]
        server.letters_adapter.gateway.complete_with_tools = plan
        await image_reply.prepare(server, row, '随手拍', row['reply_text'])
        assert packets[0]['expression_options'] == []
        assert row['image_status'] == 'COMPLETED' and len(calls['submits']) == 1
    asyncio.run(scenario())


def test_saved_paid_photo_recovers_identically_after_expression_metadata_lost(tmp_path, monkeypatch):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch)
        bind_photo_view(row)
        await image_reply._prepare_once(server, row, '随手拍', row['reply_text'])
        assert row['image_status'] == 'RETRY_PENDING' and row['image_receipt_required']
        row.pop('expression_context')
        row['reply_text'] = '另一条文字也不能让已付费照片重开订单'
        row['image_retry_at'] = 0
        await image_reply.prepare(server, row, '随手拍', row['reply_text'])
        assert row['image_status'] == 'COMPLETED' and len(calls['plans']) == 1
        assert len(calls['submissions']) == 1 and calls['statuses'] == ['saved-task']
    asyncio.run(scenario())


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
