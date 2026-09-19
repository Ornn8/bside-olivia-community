"""All direct reply surfaces must use the same frozen-evidence check."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from llm_gateway import GatewayConfig, GatewayRequestScope


def install_check(monkeypatch, events):
    async def prepare(messages, gateway, *, max_input_chars, request_id=None):
        events.append(('check', tuple(messages), gateway, max_input_chars, request_id))
        return ({**messages[0], 'content': messages[0]['content'] + '\nchecked-original-evidence'},
                *messages[1:])
    monkeypatch.setitem(sys.modules, 'runtime.memory.recall_check',
                        SimpleNamespace(prepare_recall_messages=prepare))


def adapter_with(gateway, *, max_input_chars=7000):
    from local_server import LetterAdapter
    adapter = LetterAdapter.__new__(LetterAdapter)
    adapter._runtime = (GatewayConfig(provider='mock', max_input_chars=max_input_chars, stream=True), gateway)
    adapter._messages = lambda content, context='': (
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
    install_check(monkeypatch, events)
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
    adapter = SimpleNamespace(reply_context_messages=lambda content, **kwargs: (
        {'role': 'system', 'content': 'original-persona-and-evidence'},
        {'role': 'user', 'content': content}))

    result = plan_song_content('song-letter', 'ordinary-reply', 40,
                              gateway=gateway, reply_adapter=adapter)

    assert result.duration_seconds == 40
    assert events[0][0] == 'check'
    assert events[0][2:] == (gateway, 7000, None)
    assert events[-1][0] == 'complete'
    system = events[-1][1][0]['content']
    assert 'exactly two keys: verse and chorus' in system
    assert 'original-persona-and-evidence' in system
    assert system.endswith('checked-original-evidence')
    assert json.loads(events[-1][1][1]['content'])['current_letter'] == 'song-letter'
    assert sum(event[0] == 'check' for event in events) == 1
    if scoped:
        assert ('scope', GatewayRequestScope.SONG_CONTENT) in events


@pytest.mark.parametrize('planning', [False, True])
@pytest.mark.parametrize('mode', ['text', 'voice'])
def test_proactive_body_checks_evidence_but_opportunity_planning_does_not(monkeypatch, planning, mode):
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
    adapter = adapter_with(gateway, max_input_chars=9000)
    monkeypatch.setattr(local_server, 'letters_adapter', adapter)
    monkeypatch.setattr(local_server, 'store', SimpleNamespace(letters=[{
        'letter_id': 'past', 'reply_revision': 2, 'content': 'past letter', 'reply_text': 'past reply',
    }]))

    result = asyncio.run(local_server._proactive_complete(
        {'id': 'opportunity', 'source_id': 'reply:past:2'}, planning=planning, mode=mode))

    assert result == 'proactive reply'
    assert events[-1][0] == 'complete'
    assert events[-1][2] == 'proactive:opportunity:' + ('plan' if planning else 'body')
    packet = json.loads(events[-1][1][-1]['content'])
    assert packet['previous_user_letter'] == 'past letter'
    assert packet['previous_linli_letter'] == 'past reply'
    if planning:
        assert len(events) == 1
        assert events[-1][3] is GatewayRequestScope.PROACTIVE_PLANNING
        assert 'checked-original-evidence' not in events[-1][1][0]['content']
    else:
        assert events[0][0] == 'check'
        assert events[0][2:] == (gateway, 9000, 'proactive:opportunity:body')
        assert events[-1][3] is GatewayRequestScope.BACKGROUND_REASONING
        assert 'original-persona-and-evidence' in events[-1][1][0]['content']
        assert events[-1][1][0]['content'].endswith('checked-original-evidence')
        assert sum(event[0] == 'check' for event in events) == 1
