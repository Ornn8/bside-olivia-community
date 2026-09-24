import asyncio
import json
from types import SimpleNamespace

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
