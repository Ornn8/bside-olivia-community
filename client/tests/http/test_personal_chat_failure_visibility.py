import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from runtime.personal_chat.backend import _publish_status
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


def test_generation_failure_keeps_safe_cause_and_survives_connected_restart(tmp_path):
    rows, published = [], []
    async def generate(event, row):
        raise ValueError('PERSONAL_CHAT_DECISION_INVALID')
    async def commit(row): pass
    async def send(text): raise AssertionError('must not send failed generation')
    service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})
    try:
        asyncio.run(service.handle(PersonalMessage('qq', '100', '200', '1', 'hello'), send))
    except ValueError:
        pass
    assert rows[0]['error_code'] == 'PERSONAL_CHAT_DECISION_INVALID'
    server = SimpleNamespace(store=SimpleNamespace(personal_chats=rows), _state_root=lambda: tmp_path,
                             _atomic_write_store_file=lambda path, data: published.append(json.loads(data)))
    _publish_status(server, {'status': {'qq': 'CONNECTED'}})
    assert published[-1]['errors']['qq'] == 'PERSONAL_CHAT_DECISION_INVALID'
    rows.append({'channel': 'qq', 'delivery_status': 'DELIVERED'})
    _publish_status(server, {'status': {'qq': 'CONNECTED'}})
    assert 'qq' not in published[-1]['errors']


@pytest.mark.parametrize('channel', ['qq', 'wechat'])
@pytest.mark.parametrize('notice_ack', [True, False])
def test_exhausted_generation_notifies_once_across_restart_and_keeps_next_message_working(
        tmp_path, monkeypatch, channel, notice_ack):
    import local_server
    import original_client_setup_api
    from runtime.personal_chat import backend, qq, wechat

    folder = tmp_path / 'personal-chat'
    folder.mkdir()
    secret = folder / 'credentials.dpapi'
    secret.write_text(json.dumps({'account': 'bot', 'owner': 'owner'}), encoding='utf-8')
    config = ({'qq': {'url': 'ws://127.0.0.1:3001', 'account': 'bot', 'owner': 'owner'}}
              if channel == 'qq' else {'wechat': {'credentials_file': str(secret)}})
    (folder / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    (folder / 'existing-access.json').write_text(json.dumps({'channels': [channel]}), encoding='utf-8')
    monkeypatch.delenv('OLIVIA_PERSONAL_CHAT_CONFIG', raising=False)
    monkeypatch.setenv('OLIVIA_PERSONAL_QQ_TOKEN', 'synthetic-token-123456789')
    monkeypatch.setattr(original_client_setup_api, '_dpapi_unprotect', lambda value: value)
    store, sent, generated, committed, captured = local_server.Store(), [], [], [], {}
    async def generate(server, event, row):
        generated.append(event.message_id)
        if event.message_id == 'bad':
            raise RuntimeError('PERSONAL_CHAT_REWRITE_FAILED')
        return '这条新消息可以正常回复'
    async def commit(server, row):
        committed.append(row['letter_id'])
    async def listen(*args, **kwargs):
        handler, stop = args[-2:]
        captured['handler'] = handler
        kwargs['state_callback']('CONNECTED')
        await stop.wait()
    monkeypatch.setattr(backend, 'generate', generate)
    monkeypatch.setattr(backend, 'recoverable_commit', commit)
    monkeypatch.setattr(qq, 'run_qq', listen)
    monkeypatch.setattr(wechat, 'run_wechat', listen)
    server = SimpleNamespace(store=store, _state_root=lambda: tmp_path,
        _require_store_state_available=lambda: None, _persist_store_state=lambda: None,
        _safe_log=lambda *args, **kwargs: None)
    async def send(text):
        sent.append(text)
        if '系统提示' in text and not notice_ack:
            raise RuntimeError('synthetic uncertain platform ACK')
        return 'synthetic-ack'

    async def scenario():
        bad = PersonalMessage(channel, 'bot', 'owner', 'bad', '你好')
        async def boot():
            captured.clear()
            app = web.Application()
            backend.install_personal_chat(app, server)
            runner = web.AppRunner(app)
            await runner.setup()
            for _ in range(100):
                if 'handler' in captured:
                    break
                await asyncio.sleep(0)
            assert 'handler' in captured
            return app, runner
        app, runner = await boot()
        try:
            await captured['handler'](bad, send)
            failed = store.personal_chats[0]
            assert failed['delivery_status'] == 'FAILED'
            assert failed['generation_attempts'] == 2
            assert failed['error_code'] == 'PERSONAL_CHAT_REWRITE_FAILED'
            assert generated == ['bad', 'bad']
            assert len(sent) == 1 and '系统提示' in sent[0]
            assert failed['generation_failure_notice'] == ('DELIVERED' if notice_ack else 'UNKNOWN')
            assert not failed.get('reply_text') and not failed.get('reply_revision')
            assert committed == []
            await captured['handler'](bad, send)
            assert len(sent) == 1 and generated == ['bad', 'bad']
        finally:
            await runner.cleanup()
        # Preserve only durable rows, as an application restart would.
        store.personal_chats = json.loads(json.dumps(store.personal_chats))
        app, runner = await boot()
        try:
            await captured['handler'](bad, send)
            assert len(sent) == 1 and generated == ['bad', 'bad']
            good = PersonalMessage(channel, 'bot', 'owner', 'good', '新的消息')
            await captured['handler'](good, send)
            await asyncio.sleep(0)
            assert sent[-1] == '这条新消息可以正常回复' and len(sent) == 2
            assert store.personal_chats[-1]['delivery_status'] == 'DELIVERED'
            assert committed == [good.exchange_id]
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize('status,attempts,extra,expected', [
    ('FAILED', 1, {}, False),
    ('FAILED', 2, {}, True),
    ('SENDING', 2, {}, False),
    ('DELIVERY_UNCONFIRMED', 2, {}, False),
    ('SKIPPED', 2, {}, False),
    ('FAILED', 2, {'origin': 'proactive'}, False),
    ('FAILED', 2, {'superseded_by': 'new-input'}, False),
    ('FAILED', 2, {'binding_id': 'another-owner'}, False),
])
def test_failure_notice_only_targets_exhausted_ordinary_generation(status, attempts, extra, expected):
    event = PersonalMessage('qq', 'bot', 'owner', '1', '你好')
    row = {'letter_id': event.exchange_id, 'binding_id': event.binding_id,
           'source_messages': dict(event.sources), 'delivery_status': status,
           'generation_attempts': attempts, **extra}
    sent = []
    async def unused(*args):
        raise AssertionError('must not generate or commit a notice')
    async def send(text):
        sent.append(text)
        return 'synthetic-ack'
    service = PersonalChatService([row], lambda: None, unused, unused, {'qq': ('bot', 'owner')})
    asyncio.run(service.notify_generation_failure(event, send))
    assert bool(sent) is expected
    assert row['delivery_status'] == status and not row.get('reply_text')
