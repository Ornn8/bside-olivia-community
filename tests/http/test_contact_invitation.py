import json
import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from runtime.personal_chat.contact_invitation import status, candidate, observe, validate_choice
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime


HIGH = SimpleNamespace(familiarity=70, trust=70, comfort=70, closeness=0)
LOW = SimpleNamespace(familiarity=70, trust=70, comfort=0, closeness=0)


def row(key, when):
    return {'letter_id': key, 'content': '具体交流', 'created_at': when,
            'letter_status': 'COMPLETED', 'private_world_delivery_id': key + ':1'}


def qualify(item, snapshot=HIGH, applied=True):
    observe(item, snapshot, [SimpleNamespace(event_type='meaningful_exchange', payload={
        'canonical_delivery_id': item['private_world_delivery_id'], 'applied': applied,
        'contact_qualification': snapshot is HIGH})])


def test_two_distinct_applied_exchanges_and_current_threshold():
    rows = [row('a', 1000)]
    qualify(rows[0])
    qualify(rows[0])
    assert status(rows, HIGH)['state'] == 'locked'
    rows.append(row('b', 2000))
    qualify(rows[1], applied=False)
    assert candidate(rows, HIGH, 4000) is None
    qualify(rows[1])
    assert candidate(rows, HIGH, 4000)['kind'] == 'contact_invitation'
    assert candidate(rows, LOW, 4000) is None
    rows.append(row('c', 3000))
    qualify(rows[-1], LOW)
    assert candidate(rows, HIGH, 4000) is None


def test_delivery_choice_restart_and_decline_are_separate():
    invitation = {'letter_id': 'invite', 'origin': 'proactive', 'proactive_kind': 'contact_invitation',
                  'letter_status': 'PROCESSING', 'published_at': 2000}
    rows = [invitation]
    assert status(rows, HIGH)['state'] == 'locked'
    invitation['letter_status'] = 'COMPLETED'
    assert status(rows, LOW)['state'] == 'invited'
    answer = {**row('answer', 3000), 'content': '微信吧', 'contact_invitation_id': 'invite',
              'contact_choice': {'choice': 'wechat', 'quote': '微信吧'}}
    rows.append(answer)
    restored = json.loads(json.dumps(rows))
    assert status(restored, LOW)['channels'] == ['wechat']
    assert candidate(restored, HIGH, 4000) is None
    answer.update(content='先不了', contact_choice={'choice': 'later', 'quote': '先不了'})
    assert status(rows, HIGH)['channels'] == []
    assert candidate(rows, HIGH, 4000) is None


def test_choice_needs_original_user_evidence():
    with pytest.raises(ValueError):
        validate_choice({'choice': 'wechat', 'quote': '我同意'}, '你猜我会选什么')


def test_invitation_publication_is_once_and_uses_existing_commit(monkeypatch):
    import local_server as server
    monkeypatch.setattr('runtime.personal_chat.contact_invitation.preview_configured', lambda root: True)
    rows = [row('a', 1000), row('b', 2000)]
    for item in rows:
        qualify(item)
    monkeypatch.setattr(server.store, 'letters', rows)
    monkeypatch.setattr(server, 'private_world_port', SimpleNamespace(snapshot=lambda: HIGH))
    monkeypatch.setattr(server, '_proactive_ready', lambda: True)
    monkeypatch.setattr(server, '_proactive_settings', lambda: {'enabled': True, 'allow_voice': False})
    monkeypatch.setattr(server, '_persist_store_state', lambda: None)
    commits = []
    monkeypatch.setattr(server, '_commit_private_world_letter', lambda letter: commits.append(letter['letter_id']))
    monkeypatch.setattr(server, '_schedule_daily_life_exchange', lambda letter: None)
    async def complete(*args, **kwargs):
        return '不然我们加个联系方式吧，你想用QQ还是微信？'
    monkeypatch.setattr(server, '_proactive_complete', complete)
    intent = candidate(rows, HIGH, 4000)
    asyncio.run(server._publish_proactive(intent, {'title': '聊两句', 'format': 'text'}))
    assert status(rows, HIGH)['state'] == 'invited'
    assert rows[-1]['content'] == '' and rows[-1]['origin'] == 'proactive'
    assert commits == [rows[-1]['letter_id']]
    asyncio.run(server._publish_proactive(intent, {'title': '聊两句', 'format': 'text'}))
    assert len(commits) == 1 and len(rows) == 3


def test_choice_uses_existing_extraction_and_is_durable(tmp_path):
    store = DailyLifeStore(tmp_path / 'life.sqlite3')
    # Use the real consumer with a synthetic provider, without private data.
    calls = []
    runtime = DailyLifeRuntime(store, lambda: None, lambda: '')
    async def complete(prompt, data, request_id):
        calls.append(data)
        return {'updates': [], 'contact_choice': {'choice': 'wechat', 'quote': '微信吧'}}
    runtime._complete = complete
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    asyncio.run(runtime.consume_exchange('reply:test:1', '微信吧', '好啊。', occurred_at=now, contact_invited=True))
    assert len(calls) == 1
    assert store._exchange_payload('reply:test:1', '微信吧', '好啊。')['contact_choice']['choice'] == 'wechat'
    asyncio.run(runtime.consume_exchange('reply:test:1', '微信吧', '好啊。', occurred_at=now, contact_invited=True))
    assert len(calls) == 1
    with pytest.raises(ValueError):
        asyncio.run(runtime.consume_exchange('reply:test:2', '微信吧', '好啊。', occurred_at=now))
def test_preview_invitation_requires_explicit_local_configuration(tmp_path):
    from runtime.personal_chat.contact_invitation import preview_configured
    assert not preview_configured(tmp_path, {})
    assert not preview_configured(None, {})
    path = tmp_path / 'personal-chat/config.json'
    path.parent.mkdir()
    path.write_text('{}', encoding='utf-8')
    assert preview_configured(tmp_path, {})
    assert not preview_configured(tmp_path, {'OLIVIA_PERSONAL_CHAT_CONFIG': str(tmp_path / 'missing.json')})
