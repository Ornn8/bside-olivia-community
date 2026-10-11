import asyncio
import hashlib
import pytest

from aiohttp import web
from aiohttp.test_utils import TestServer

from runtime.remote_generation import RemoteGeneration
from tests.http.test_piano_qq import META
from tests.media.test_piano import CAPS


def test_upload_submit_poll_meta_and_receipt_recovery(tmp_path, monkeypatch):
    async def scenario():
        uploaded = []; submitted = []; polls = []
        midi = b'MThdsynthetic'; digest = hashlib.sha256(midi).hexdigest()
        async def handler(request):
            if request.path in {'/v1/capabilities', '/v1/tasks'}:
                assert request.headers['X-Olivia-Piano'] == '1'
            if request.path == '/v1/capabilities':
                return web.json_response(dict(kinds=[], shared_assets=[], piano=CAPS, result_acknowledgement=True))
            if request.path == '/v1/assets':
                uploaded.append(await request.read())
                assert request.headers['X-Asset-Suffix'] == '.mid'
                return web.json_response({'asset_id': digest}, status=201)
            if request.path == '/v1/tasks':
                assert request.headers['X-Olivia-Max-Charge-Cents'] == str(max(v[1] for v in CAPS['price_ranges_cents'].values()))
                submitted.append(await request.json())
                return web.json_response(dict(task_id='piano-test', status='queued'))
            if request.path == '/v1/tasks/piano-test':
                polls.append(True)
                return web.json_response(dict(task_id='piano-test', status='succeeded', piano_meta={**META, 'future': True},
                    outputs=[dict(url=str(server.make_url('/result')))]))
            assert request.path == '/result'
            return web.Response(body=b'synthetic-mp3')
        async def sleep(seconds): pass
        monkeypatch.setattr('runtime.remote_generation.asyncio.sleep', sleep)
        app = web.Application(); app.router.add_route('*', '/{tail:.*}', handler)
        source = tmp_path / 'input.mid'; source.write_bytes(midi)
        async with TestServer(app) as server:
            api = RemoteGeneration(str(server.make_url('/')), 'synthetic')
            output = tmp_path / 'result.mp3'; receipt = tmp_path / 'receipt.json'
            for _ in range(2):
                result = await api.generate('piano', dict(source='midi', title='合成曲目', seconds=30), output,
                                             assets={'midi_asset': source}, receipt_path=receipt)
                assert result['piano_meta'] == META
                assert output.read_bytes() == b'synthetic-mp3'
        assert uploaded == [midi] and len(submitted) == 1 and len(polls) == 2
        assert submitted[0]['input'] == dict(source='midi', midi_asset=digest, title='合成曲目', seconds=30)
    asyncio.run(scenario())


def test_damaged_piano_receipt_never_creates_a_second_order(tmp_path, monkeypatch):
    from runtime.cloud_service import CloudError
    async def request(self, action, data):
        assert action == 'capabilities'
        return dict(kinds=['piano'], shared_assets=[], piano=CAPS)
    monkeypatch.setattr(RemoteGeneration, 'request', request)
    receipt = tmp_path / 'receipt.json'; receipt.write_text('damaged', encoding='utf-8')
    with pytest.raises(CloudError, match='GPU_RECOVERY_REQUIRED'):
        asyncio.run(RemoteGeneration().generate('piano', dict(source='original', title='夜曲', mood='平静', seconds=30),
            tmp_path / 'out.mp3', receipt_path=receipt))
