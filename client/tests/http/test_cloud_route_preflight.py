import asyncio

import pytest

from runtime.cloud_service import CloudError
from runtime import remote_pipeline
from runtime.video_reply_settings import VideoReplySettingsStore


@pytest.mark.parametrize('failure', ['GPU_CONNECTION_TIMEOUT', 'GPU_TLS_FAILED', 'GPU_AUTH_FAILED', 'private detail', None])
def test_cloud_cover_failure_is_distinct_from_local_components(tmp_path, monkeypatch, failure):
    import local_server as server
    settings = VideoReplySettingsStore.initialize(tmp_path)
    settings.mutate_tier('video_reply_setting:cloud-test', 'audio')
    monkeypatch.setattr(server, 'video_reply_settings_store', settings)
    monkeypatch.setattr(server, '_reply_route_previews', {})
    monkeypatch.setattr(server, '_missing_memory_component', lambda: None)
    monkeypatch.setattr(server, '_voice_reply_configured', lambda *a: pytest.fail('local probe'))
    monkeypatch.setenv('OLIVIA_GPU_ROUTE', 'remote')
    monkeypatch.setenv('OLIVIA_LOCAL_DATA_ROOT', str(tmp_path))
    source = tmp_path / 'cover-inputs' / ('a' * 32) / 'source.wav'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'synthetic')
    calls = []
    def capabilities(_):
        calls.append(True)
        if failure:
            raise CloudError(failure)
        return {'kinds': ['tts']}
    monkeypatch.setattr(remote_pipeline, 'capabilities', capabilities)
    body = {'content': '请翻唱', 'cover_source_id': 'a' * 32, 'cover_output': 'audio'}
    result = asyncio.run(server.route('POST', '/toy/letter/route-preview', body, {}))
    assert result['code'] == 0, result
    data = result['data']
    assert data['ready'] is False
    assert data['readiness']['backend'] == 'remote'
    expected = failure if failure and failure.startswith('GPU_') else 'GPU_CAPABILITY_UNAVAILABLE' if failure is None else 'GPU_REQUEST_FAILED'
    assert data['readiness']['error_code'] == expected
    assert 'private detail' not in str(result)
    assert len(calls) == 1
    monkeypatch.setattr(remote_pipeline, 'capabilities', lambda _: {'kinds': ['cover']})
    recovered = asyncio.run(server.route('POST', '/toy/letter/route-preview', body, {}))['data']
    assert recovered['ready'] is True
    assert not recovered['readiness'].get('error_code')
    # A failure between preview and send must stay cloud-specific and save no letter.
    monkeypatch.setattr(remote_pipeline, 'capabilities', capabilities)
    monkeypatch.setattr(server.store, 'letters', [])
    monkeypatch.setattr(server.store, 'request_keys', {})
    monkeypatch.setattr(server, '_persist_store_state', lambda: pytest.fail('persisted failed preflight'))
    monkeypatch.setattr(server, '_schedule_reply_job', lambda *a, **kw: pytest.fail('submitted failed preflight'))
    result = asyncio.run(server.route('POST', '/toy/letter/send', {
        'content': body['content'], 'material': {
            'cover_source_id': body['cover_source_id'], 'cover_output': 'audio',
            'route_preview_token': recovered['token'],
        }}, {}, defer_reply=True))
    assert result['data']['error_code'] == expected
    assert not server.store.letters
