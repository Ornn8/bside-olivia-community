import asyncio
import local_server


def test_background_import_survives_submit_and_deduplicates(monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        original = local_server.route

        async def route(*args, **kwargs):
            if kwargs.get('_local_import_worker'):
                calls.append(1)
                entered.set()
                await release.wait()
                return local_server.ok({'status': 'APPLIED', 'inserted': 14})
            return await original(*args, **kwargs)

        monkeypatch.setattr(local_server, 'route', route)
        monkeypatch.setattr(local_server, '_local_import_task', None, raising=False)
        monkeypatch.setattr(local_server, '_local_import_result', None, raising=False)
        path = '/toy/letter/legacy/local-import'
        first = await route('POST', path, {'background': True}, {}, companion_confirmed=True)
        assert first['data']['status'] == 'RUNNING'
        await entered.wait()
        again = await route('POST', path, {'background': True}, {}, companion_confirmed=True)
        assert again['data']['status'] == 'RUNNING'
        assert calls == [1]
        release.set()
        await local_server._local_import_task
        done = await route('GET', path, {}, {'progress': '1'})
        assert done['data']['status'] == 'APPLIED'
        assert done['data']['inserted'] == 14
    asyncio.run(scenario())
