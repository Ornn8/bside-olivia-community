"""The sources an update restart waits for: replies on QQ/WeChat and letters, never background jobs."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from runtime import auto_update


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(auto_update, '_reply_sources', {})
    monkeypatch.setattr(auto_update, 'REPLY_POLL_SECONDS', 0.01)


def test_letter_replies_being_written_hold_the_restart_and_other_jobs_do_not(monkeypatch):
    import local_server

    async def idle(*args, **kwargs):
        await asyncio.Event().wait()
    for name in ('_letter_trash_loop', '_proactive_loop', '_visit_loop', '_recover_photo_memories',
                 '_relationship_batch_loop', '_vector_index_loop', '_improve_loop', '_diary_loop',
                 '_refresh_daily_life_periodically'):
        monkeypatch.setattr(local_server, name, idle)
    monkeypatch.setattr(local_server, '_schedule_pending_reply_jobs', lambda: 0)
    monkeypatch.setattr(local_server, '_schedule_pending_media_jobs', lambda: 0)
    monkeypatch.setattr(local_server, 'reply_jobs', {})

    async def scenario():
        assert not auto_update.replies_in_flight()
        await local_server._start_reply_tasks(web.Application())
        try:
            assert not auto_update.replies_in_flight()
            # Diary, world and relationship work are background jobs: she is not "answering" while they run.
            background = asyncio.create_task(idle())
            local_server.media_tasks.add(background)
            local_server.media_jobs['a-media-job'] = background
            assert not auto_update.replies_in_flight()
            finished = asyncio.create_task(asyncio.sleep(0))
            await finished
            local_server.reply_jobs['done'] = finished
            assert not auto_update.replies_in_flight()
            writing = asyncio.create_task(idle())
            local_server.reply_jobs['a-letter'] = writing
            assert auto_update.replies_in_flight()
            writing.cancel()
            await asyncio.gather(writing, return_exceptions=True)
            assert not auto_update.replies_in_flight()
        finally:
            await local_server._stop_reply_tasks(web.Application())
        assert 'letters' not in auto_update._reply_sources
    asyncio.run(scenario())


def test_the_chat_backend_registers_its_replies_while_it_runs(tmp_path, monkeypatch):
    import local_server
    from runtime.personal_chat import backend, qq

    monkeypatch.setattr(backend, 'selected_channels', lambda server: {'qq'})
    config = tmp_path / 'channels.json'
    config.write_text(json.dumps({'qq': {'url': 'ws://127.0.0.1:3001', 'account': '10000', 'owner': '20000'}}))
    monkeypatch.setenv('OLIVIA_PERSONAL_CHAT_CONFIG', str(config))
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    server = SimpleNamespace(store=local_server.Store(), _state_root=lambda: tmp_path,
        _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
        _safe_log=lambda event, **fields: None)

    async def no_recovery(*args):
        pass
    monkeypatch.setattr(backend, '_recover_chat_loop', no_recovery)

    async def scenario():
        listening = asyncio.Event()

        async def listen(*args, **kwargs):
            kwargs['state_callback']('CONNECTED')
            listening.set()
            await args[-1].wait()
        monkeypatch.setattr(qq, 'run_qq', listen)
        app = web.Application()
        backend.install_personal_chat(app, server)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await asyncio.wait_for(listening.wait(), 5)
            service = server._personal_chat_service
            assert 'personal_chat' in auto_update._reply_sources and not auto_update.replies_in_flight()
            service.handling += 1                     # a message is being answered
            assert auto_update.replies_in_flight()
            service.handling -= 1
            assert not auto_update.replies_in_flight()
        finally:
            await runner.cleanup()
        assert 'personal_chat' not in auto_update._reply_sources
    asyncio.run(scenario())


class Updater:
    def __init__(self):
        self.restarts = []

    def apply(self, package, manifest_sha256):
        return {'status': 'APPLIED', 'component': 'local_backend', 'version': '1.2.3'}

    def rollback(self):
        return {'status': 'ROLLED_BACK', 'component': 'local_backend', 'version': '1.2.2'}

    def restart(self):
        self.restarts.append(1)


def test_the_hand_picked_patch_restart_waits_for_the_reply_too(tmp_path, monkeypatch):
    import original_client_update_api as api
    monkeypatch.setattr(api, '_exit_for_restart', lambda: None)
    origin = 'https://client.example'
    headers = {'Origin': origin, api.CONFIRM_HEADER: 'confirmed', api.SESSION_HEADER: 'signed-in'}

    async def scenario():
        package = tmp_path / 'olivia-1.2.3.oliviapatch'
        package.write_bytes(b'fixture')
        updater = Updater()
        app = web.Application()
        api.mount_original_client_update_api(app, updater, trusted_origins=(origin,), authorize_session=lambda _: None)
        writing = {'now': True}
        auto_update.track_replies('personal_chat', lambda: writing['now'])
        async with TestClient(TestServer(app)) as client:
            applied = await client.post(api.ACTION_PATH, headers=headers,
                json={'action': 'apply', 'package_path': str(package), 'manifest_sha256': 'a' * 64})
            assert applied.status == 200
            restarting = asyncio.create_task(client.post(api.RESTART_PATH, headers=headers, json={}))
            await asyncio.sleep(0.15)
            assert not restarting.done() and updater.restarts == []
            writing['now'] = False
            response = await asyncio.wait_for(restarting, 3)
            assert response.status == 202 and (await response.json()) == {'status': 'RESTARTING'}
            assert updater.restarts == [1]
    asyncio.run(scenario())


def test_the_hand_picked_patch_restart_goes_at_once_when_nothing_is_being_written(tmp_path, monkeypatch):
    import original_client_update_api as api
    monkeypatch.setattr(api, '_exit_for_restart', lambda: None)
    origin = 'https://client.example'
    headers = {'Origin': origin, api.CONFIRM_HEADER: 'confirmed', api.SESSION_HEADER: 'signed-in'}

    async def scenario():
        package = tmp_path / 'olivia-1.2.3.oliviapatch'
        package.write_bytes(b'fixture')
        updater = Updater()
        app = web.Application()
        api.mount_original_client_update_api(app, updater, trusted_origins=(origin,), authorize_session=lambda _: None)
        async with TestClient(TestServer(app)) as client:
            await client.post(api.ACTION_PATH, headers=headers,
                json={'action': 'apply', 'package_path': str(package), 'manifest_sha256': 'a' * 64})
            response = await asyncio.wait_for(client.post(api.RESTART_PATH, headers=headers, json={}), 1)
            assert response.status == 202 and updater.restarts == [1]
    asyncio.run(scenario())
