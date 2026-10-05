"""A damaged video receipt must never become a new paid media order."""
import asyncio
import json
import pytest
from runtime.cloud_service import CloudError
from runtime.remote_generation import RemoteGeneration


@pytest.mark.parametrize('kind', ['video', 'lipsync', 'cover_video', 'original_video'])
@pytest.mark.parametrize('damage', [
    'fingerprint', 'json', 'encoding', 'not_mapping', 'submission', 'request_id', 'task_id',
])
def test_invalid_video_receipt_blocks_new_order_and_preserves_evidence(tmp_path, monkeypatch, kind, damage):
    api = RemoteGeneration('https://gpu.example', 'synthetic-key')
    receipt = tmp_path / 'order.json'
    output = tmp_path / 'output.mp4'
    source = tmp_path / 'source.wav'
    source.write_bytes(b'synthetic-audio')
    calls = []
    async def request(action, data):
        calls.append(action)
        if action == 'capabilities':
            return {'kinds': [kind], 'shared_assets': []}
        assert action == 'submit'
        # The server may already have accepted this order. Retain its original key.
        raise CloudError('GPU_CONNECTION_FAILED')
    async def upload(path):
        calls.append('upload')
        return 'original-asset'
    async def download(task, target, **kwargs):
        calls.append('download')
        return task
    monkeypatch.setattr(api, 'request', request)
    monkeypatch.setattr(api, 'upload', upload)
    monkeypatch.setattr(api, '_download', download)
    def generate():
        return asyncio.run(api.generate(kind, {'text': 'frozen-letter'}, output,
            assets={'source_asset': source}, receipt_path=receipt))
    with pytest.raises(CloudError, match='GPU_CONNECTION_FAILED'):
        generate()
    saved = json.loads(receipt.read_text(encoding='utf-8'))
    if damage == 'fingerprint':
        saved['fingerprint'] = 'changed'
    elif damage == 'submission':
        saved['submission'] = {}
    elif damage == 'request_id':
        saved['submission']['request_id'] = ''
    elif damage == 'task_id':
        saved['task_id'] = []
    raw = (b'{' if damage == 'json' else b'\xff' if damage == 'encoding'
        else b'[]' if damage == 'not_mapping' else json.dumps(saved).encode())
    receipt.write_bytes(raw)
    output.write_bytes(b'previous-valid-video')
    calls.clear()
    def no_uuid():
        pytest.fail('Recovery must not create another idempotency key')
    monkeypatch.setattr('runtime.remote_generation.uuid.uuid4', no_uuid)
    with pytest.raises(CloudError, match='GPU_RECOVERY_REQUIRED'):
        generate()
    assert calls == ['capabilities']
    assert receipt.read_bytes() == raw
    assert output.read_bytes() == b'previous-valid-video'
