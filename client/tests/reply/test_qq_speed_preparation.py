import asyncio
from datetime import datetime, timezone
import json

from runtime.reply import world_context_selection as world
from runtime.reply import conversation_context as conversation


def test_large_world_trimming_does_not_reserialize_whole_catalog(monkeypatch):
    original = world._json
    serialized_catalogs = []
    def encode(value):
        if isinstance(value, dict) and 'questions' in value:
            serialized_catalogs.append(len(value['questions']))
        return original(value)
    monkeypatch.setattr(world, '_json', encode)
    packet = dict(base={'as_of': '2026-10-11'}, rhythm={}, records=[
        dict(field='threads', many=True, value=dict(id=str(i), title='合成事项' * 20,
            status='completed' if i < 2500 else 'ongoing', updated_at=f'{i:04}')) for i in range(3000)])
    class Port:
        async def ask(self, state, questions, *, purpose):
            assert len(questions) <= 192
            assert len(original(dict(state=state, questions=questions, purpose=purpose)).encode()) <= 30000
            assert 'r2999' in questions and 'r0' not in questions
            return {key: 'skip' for key in questions}
    # The default candidate cap would hide the size trimming measured here, so lift it.
    asyncio.run(world.select_world_context(Port(), packet, '合成问题', max_candidates=len(packet['records'])))
    assert len(serialized_catalogs) <= 3


def test_world_recent_dialogue_skips_unused_archive_without_changing_recent(monkeypatch):
    from runtime.personal_chat.context import READ_WINDOW
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    rows = [dict(letter_id=str(i), channel='qq', delivery_status='DELIVERED',
        letter_status='COMPLETED', reply_revision=1, content='早上好', reply_text='你好',
        created_at=i, private_world_occurred_at=now.isoformat()) for i in range(3150)]
    calls = []
    def history(*args, **kwargs):
        calls.append(1)
        return ''
    monkeypatch.setattr(conversation, 'recent_correspondence', history)
    token = READ_WINDOW.set(rows)
    try:
        expected, _ = conversation.conversation_context((), query='你好', now=now)
        actual, old = conversation.conversation_context((), query='你好', now=now, include_history=False)
    finally:
        READ_WINDOW.reset(token)
    assert calls == [1] and actual == expected and old == ''
    assert json.loads(actual)['letters']
