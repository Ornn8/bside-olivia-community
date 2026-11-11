"""All direct reply surfaces must use the same frozen-evidence check."""
import asyncio
from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace

import pytest

from llm_gateway import GatewayConfig, GatewayRequestScope


def install_check(monkeypatch, events):
    bindings = []
    async def prepare(messages, gateway, *, max_input_chars, request_id=None,
                      memory_builder=None, as_of=None, exclude_source_ids=(),
                      current_source_ids=(), current_user_text=None,
                      persona_snapshot=None, persona_mode=None, persona_development=None):
        events.append(('check', tuple(messages), gateway, max_input_chars, request_id))
        bindings.append({'memory_builder': memory_builder, 'as_of': as_of,
                         'exclude_source_ids': exclude_source_ids,
                         'current_source_ids': current_source_ids,
                         'current_user_text': current_user_text,
                         'persona_snapshot': persona_snapshot, 'persona_mode': persona_mode})
        return ({**messages[0], 'content': messages[0]['content'] + '\nchecked-original-evidence'},
                *messages[1:])
    monkeypatch.setitem(sys.modules, 'runtime.memory.history_selection',
                        SimpleNamespace(select_history_messages=prepare))
    return bindings


def adapter_with(gateway, *, max_input_chars=7000):
    from local_server import LetterAdapter
    adapter = LetterAdapter.__new__(LetterAdapter)
    adapter._runtime = (GatewayConfig(provider='mock', max_input_chars=max_input_chars, stream=True,
                                     persona_v2_enabled=False), gateway)
    adapter._messages = lambda content, context='', **kwargs: (
        {'role': 'system', 'content': 'original-persona-and-evidence'},
        {'role': 'user', 'content': content + context})
    return adapter


@pytest.mark.parametrize('scoped', [False, True])
def test_direct_reply_checks_frozen_messages_before_generation(monkeypatch, scoped):
    events = []
    install_check(monkeypatch, events)
    class Gateway:
        async def complete(self, messages, *, request_id=None):
            events.append(('complete', tuple(messages), request_id))
            return SimpleNamespace(text='reply')
        async def complete_scoped(self, messages, *, request_id=None, scope):
            events.append(('scope', scope))
            return await self.complete(messages, request_id=request_id)
    gateway = Gateway()
    adapter = adapter_with(gateway)
    scope = GatewayRequestScope.SONG_CONTENT if scoped else None

    assert adapter.reply('letter', '-context', request_id='direct-1', gateway_scope=scope) == 'reply'

    assert events[0][0] == 'check'
    assert events[0][2:] == (gateway, 7000, 'direct-1')
    assert events[0][1][0]['content'] == 'original-persona-and-evidence'
    assert events[-1][0] == 'complete'
    assert events[-1][1][0]['content'].endswith('checked-original-evidence')
    assert events[-1][1][1] == {'role': 'user', 'content': 'letter-context'}
    assert events[-1][2] == 'direct-1'
    assert sum(event[0] == 'check' for event in events) == 1
    if scoped:
        assert ('scope', scope) in events


@pytest.mark.parametrize('scoped', [False, True])
def test_stream_checks_rebuilt_messages_before_first_delta(monkeypatch, scoped):
    from local_server import _LetterGateway
    events = []
    install_check(monkeypatch, events)
    class Gateway:
        async def stream(self, messages, *, request_id=None):
            events.append(('stream', tuple(messages), request_id))
            yield SimpleNamespace(text='delta', request_id=request_id, index=0, finish_reason='stop')
        async def stream_scoped(self, messages, *, request_id=None, scope):
            events.append(('scope', scope))
            async for delta in self.stream(messages, request_id=request_id):
                yield delta
    gateway = Gateway()
    bridge = _LetterGateway(adapter_with(gateway))
    scope = GatewayRequestScope.SONG_CONTENT
    messages = ({'role': 'user', 'content': 'stream-letter'},)
    async def collect():
        stream = (bridge.stream_scoped(messages, request_id='stream-1', scope=scope)
                  if scoped else bridge.stream(messages, request_id='stream-1'))
        return [delta async for delta in stream]

    result = asyncio.run(collect())

    assert [delta.text for delta in result] == ['delta']
    assert events[0][0] == 'check'
    assert events[0][2:] == (gateway, 7000, 'stream-1')
    assert events[-1][0] == 'stream'
    assert events[-1][1][0]['content'].endswith('checked-original-evidence')
    assert events[-1][1][1] == {'role': 'user', 'content': 'stream-letter'}
    assert sum(event[0] == 'check' for event in events) == 1
    if scoped:
        assert ('scope', scope) in events


@pytest.mark.parametrize('scoped', [False, True])
def test_song_checks_shared_evidence_and_preserves_lyric_contract(monkeypatch, scoped):
    from runtime.media.song_content import plan_song_content
    events = []
    bindings = install_check(monkeypatch, events)
    payload = json.dumps({'verse': ['把今天的信轻轻收好'] * 6, 'chorus': ['让这盏灯为你亮着'] * 6}, ensure_ascii=False)
    class Gateway:
        config = GatewayConfig(provider='mock', max_input_chars=7000)
        async def complete(self, messages):
            events.append(('complete', tuple(messages)))
            return SimpleNamespace(text=payload)
    class ScopedGateway(Gateway):
        async def complete_scoped(self, messages, *, scope):
            events.append(('scope', scope))
            return await self.complete(messages)
    gateway = ScopedGateway() if scoped else Gateway()
    builder = object()
    adapter = SimpleNamespace(memory_prompt_builder=builder,
        _memory_source_exclusions=lambda: ('reply:current-song:1',),
        reply_context_messages=lambda content, **kwargs: (
        {'role': 'system', 'content': 'original-persona-and-evidence'},
        {'role': 'user', 'content': content}))

    result = plan_song_content('song-letter', 'ordinary-reply', 40,
                              gateway=gateway, reply_adapter=adapter)

    assert result.duration_seconds == 40
    assert events[0][0] == 'check'
    assert events[0][2:] == (gateway, 7000, None)
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding['memory_builder'] is builder
    assert binding['as_of'].utcoffset() is not None
    assert binding['exclude_source_ids'] == binding['current_source_ids'] == ('reply:current-song:1',)
    assert binding['current_user_text'] == 'song-letter'
    assert events[-1][0] == 'complete'
    system = events[-1][1][0]['content']
    assert 'exactly two keys: verse and chorus' in events[-1][1][-2]['content']
    assert 'original-persona-and-evidence' in system
    assert system.endswith('checked-original-evidence')
    assert json.loads(events[-1][1][-1]['content'])['current_letter'] == 'song-letter'
    assert sum(event[0] == 'check' for event in events) == 1
    if scoped:
        assert ('scope', GatewayRequestScope.SONG_CONTENT) in events


@pytest.mark.parametrize('planning', [False, True])
@pytest.mark.parametrize('mode', ['text', 'voice'])
def test_proactive_planning_and_body_use_the_same_prepared_evidence(monkeypatch, planning, mode):
    import local_server
    events = []
    install_check(monkeypatch, events)
    class Gateway:
        async def complete_scoped(self, messages, *, request_id, scope):
            events.append(('complete', tuple(messages), request_id, scope))
            return SimpleNamespace(text='proactive reply')
        def timeout_seconds_for_scope(self, scope, *, default):
            return 5
    gateway = Gateway()
    adapter = local_server.LetterAdapter(GatewayConfig(
        provider='mock', max_input_chars=9000, persona_v2_enabled=False))
    adapter.gateway = gateway
    adapter.persona_provider = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(system_prompt='original-persona-and-evidence'),
        messages_for=lambda content, **kwargs: (
            {'role': 'system', 'content': 'original-persona-and-evidence'},
            {'role': 'user', 'content': content}))
    monkeypatch.setattr(local_server, 'letters_adapter', adapter)
    monkeypatch.setattr(local_server, 'store', SimpleNamespace(letters=[{
        'letter_id': 'past', 'reply_revision': 2, 'content': 'past letter', 'reply_text': 'past reply',
        'letter_status': 'COMPLETED',
    }], personal_chats=[]))

    intent = {'id': 'opportunity', 'source_id': 'reply:past:2'}
    async def exercise():
        turn = await local_server._prepare_proactive_turn(intent, now=datetime.now(timezone.utc))
        result = await local_server._proactive_complete(intent, planning=planning, mode=mode, turn=turn)
        return result, turn
    result, turn = asyncio.run(exercise())

    assert result == 'proactive reply'
    assert events[-1][0] == 'complete'
    assert events[-1][2] == 'proactive:opportunity:' + ('plan' if planning else 'body')
    packet = json.loads(events[-1][1][-1]['content'])
    assert packet['previous_user_letter'] == 'past letter'
    assert packet['previous_linli_letter'] == 'past reply'
    assert sum(len(message['content']) for message in events[-1][1]) <= 9000
    assert sum(message['content'] == turn['packet'] for message in events[-1][1]) == 1
    assert len(events) == 2
    assert events[0][0] == 'check'
    task_reserve = max(len(local_server._proactive_instruction(intent, planning=True)),
        *(len(local_server._proactive_instruction(intent, planning=False, mode=m, photo=photo))
          for m in ('text', 'voice') for photo in (True, False)))
    recall_budget = 9000 - task_reserve - sum(map(len, turn['common_blocks']))
    assert events[0][2:] == (gateway, recall_budget, 'proactive:opportunity:context')
    assert 'original-persona-and-evidence' in events[-1][1][0]['content']
    assert events[-1][1][0]['content'].endswith('checked-original-evidence')
    assert sum(event[0] == 'check' for event in events) == 1
    if planning:
        assert events[-1][3] is GatewayRequestScope.PROACTIVE_PLANNING
    else:
        assert events[-1][3] is GatewayRequestScope.BACKGROUND_REASONING
