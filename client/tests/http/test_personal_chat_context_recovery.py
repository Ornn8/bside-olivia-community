import asyncio
from copy import deepcopy

from runtime.personal_chat.events import PersonalMessage
from tests.http.test_personal_chat_provider_recovery import _fixture
from tests.persona.test_companion_orphan_requirements import pending
from tests.persona.test_jev_pipeline import Port


def test_context_recovery_is_persisted_before_new_decision_and_next_turn_delivers(tmp_path, monkeypatch):
    server, calls, sent, committed, persisted, captured, send, boot = _fixture(tmp_path, monkeypatch, [])
    port = Port()
    server.reply_pipeline.companion_decision_port = port
    old_event = PersonalMessage('qq', '100', '200', 'old', 'old input')
    old = dict(letter_id='old', channel='qq', binding_id=old_event.binding_id,
        created_at=1, received_sequence=1, delivery_status='DELIVERED', letter_status='COMPLETED',
        content='old input', reply_text='old answer', reply_revision=1,
        companion_decision=pending('reply:missing:user'))
    server.store.personal_chats.append(old)
    before = deepcopy(old)

    async def scenario():
        app, runner = await boot()
        try:
            for number in range(2):
                event = PersonalMessage('qq', '100', '200', f'new{number}', f'new input {number}')
                await captured['handler'](event, send)
                await asyncio.sleep(0)
                row = server.store.personal_chats[-1]
                assert row['delivery_status'] == 'DELIVERED', row.get('error_code')
                assert row['companion_decision']['input']['messages'][-1]['text'] == event.text
            assert server.store.personal_chats[0] == before
            recovered = server.store.personal_chats[1]
            expected = [dict(requirement_index=0, status='abandoned', reason='required_source_missing')]
            assert recovered['companion_context_recovery'] == expected
            assert any(rows[-1].get('companion_context_recovery') == expected
                       and 'companion_decision' not in rows[-1] for rows in persisted)
            assert 'companion_context_recovery' not in server.store.personal_chats[2]
            assert len(port.turns) == len(calls) == len(sent) == 2
            assert committed[-2:] == [r['letter_id'] for r in server.store.personal_chats[1:]]
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
