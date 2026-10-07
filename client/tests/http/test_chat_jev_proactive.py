"""Development-only proactive IM decisions retain live delivery boundaries."""
import asyncio
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


def environment(monkeypatch, tmp_path, *, action='send', medium='text', body=None, writer_hook=None):
    from llm_gateway import GatewayResponse
    from runtime.personal_chat import backend
    from runtime.reply import companion_duties, companion_proactive
    from persona_assembly import UntrustedFragment
    from tests.persona.test_reply_pipeline import _configured_v2_pipeline
    from tests.persona.test_persona_context_selection import RELEASE
    from tests.persona.test_companion_proactive import envelope as response
    from tests.http.test_personal_chat_decision import envelope
    from tests.persona.test_jev_pipeline import Port

    monkeypatch.setenv('OLIVIA_JEV_DECISION_URL', 'http://127.0.0.1:8097/v1/companion/decide')
    pipeline, _, bridge, provider = _configured_v2_pipeline(RELEASE)
    adapter = bridge.adapter
    now = datetime.now(timezone.utc)
    snapshot = dict(kind='character_life_reference', stale=False, current={
        'source_id':'day:read', 'occurred_at':now.isoformat(), 'activity_kind':'reading', 'note':'读完一篇散文',
        'location':'宿舍', 'activity':'读书'},
        world={'schedule':{'current_class':None}}, rhythm={'phase':'awake', 'availability':'open'})
    emotion = dict(interpretation_only=True, reaction_subject='character', reactions=[], concerns=[],
                   current_affect={'label':'relieved', 'reason':'读完后轻松一些', 'status':'available'})
    async def appraise(content, **kwargs):
        assert content is None
        return deepcopy(emotion)
    adapter.prepare_character_emotion = appraise
    def life(*a, **k):
        # DailyLifeStore.context projects current fields and schedule separately.
        projected = dict(kind='character_life_reference', stale=False,
            current={key:snapshot['current'][key] for key in ('source_id','occurred_at','location','activity','note')},
            schedule=deepcopy(snapshot['world']['schedule']))
        return (UntrustedFragment('linli.daily-life', json.dumps(projected, ensure_ascii=False)),)
    adapter.daily_life_fragments = life
    calls, writes, saves, audio, photos, sent, committed = [], [], [], [], [], [], []
    rows = []
    async def persona(kind, value):
        assert kind == 'persona'
        return SimpleNamespace(decision={'persona_ids':[]}, error_code=None)
    monkeypatch.setattr(companion_duties, 'configured_duties', lambda: SimpleNamespace(evaluate=persona))
    user_port = pipeline.companion_decision_port = Port()
    port = companion_proactive.JevProactivePort()
    def request(encoded):
        value = json.loads(encoded)
        calls.append(value)
        decision = (dict(action='defer', opportunity_id=None, reason='not_now', intent=None, medium=None)
                    if action == 'defer' else dict(action='send', opportunity_id=None,
                        reason='spontaneous_connection', intent='connection', medium=medium))
        return response(value, decision)
    monkeypatch.setattr(port, '_request', request)
    monkeypatch.setattr(companion_proactive, 'configured_proactive', lambda: port)
    async def complete(messages, **kwargs):
        assert rows[0]['proactive_decision']['decision']['action'] == 'send'
        assert any(item and item[0].get('proactive_decision') for item in saves)
        writes.append(deepcopy(messages))
        if writer_hook: writer_hook(snapshot)
        return GatewayResponse(text=body if body is not None else envelope(text='读到一句很喜欢的话，想跟你说说',
            delivery='voice' if medium == 'text' else 'text', sticker='linli-29', letter_invitation=True),
            request_id=kwargs.get('request_id') or 'proactive', provider='synthetic', model='synthetic')
    provider.complete = complete
    private = SimpleNamespace(relationship_stage='close', tension=0)
    server = SimpleNamespace(letters_adapter=adapter, reply_pipeline=pipeline,
        _llm_runtime_ready=lambda _: True, daily_life_runtime=SimpleNamespace(store=SimpleNamespace(
            snapshot=lambda now: deepcopy(snapshot))), private_world_port=SimpleNamespace(snapshot=lambda:private),
        _official_history_private_world_available=lambda:True, MEMORY_READY_REPLY_TIMEOUT_SECONDS=1,
        _conversation_memory_ready_for_reply=lambda:True,
        _CURRENT_LETTER_MEMORY_SOURCE=ContextVar('proactive_source'),
        _CURRENT_LETTER_RECEIPT=ContextVar('proactive_receipt'),
        supports_scoped_reasoning=lambda _:False, _reply_pipeline_timeout_seconds=lambda _:5,
        store=SimpleNamespace(personal_chats=rows, letters=[]), _state_root=lambda:tmp_path,
        _voice_reply_configured=lambda _:True, _safe_log=lambda *a, **k:None,
        _persist_store_state=lambda:saves.append(deepcopy(rows)),
        video_reply_settings_store=SimpleNamespace(image_snapshot=lambda:{'enabled':True}))
    async def render(server, text, path, **kwargs):
        audio.append(text)
        return {'duration_seconds':2}
    monkeypatch.setattr(backend, 'prepare_chat_audio', render)
    async def generate(event, row): return await backend.generate(server, event, row)
    async def commit(row): committed.append(row['letter_id'])
    async def photo(row, send): photos.append(row['letter_id'])
    class Sender:
        async def __call__(self, text): sent.append(('text', text)); return 'ack'
        async def audio(self, path): sent.append(('audio', path)); return 'ack'
        async def image(self, path): photos.append(path); return 'ack'
    service = PersonalChatService(rows, lambda:None, generate, commit, {'qq':('bot','owner')}, photo=photo)
    return SimpleNamespace(server=server, service=service, sender=Sender(), rows=rows, calls=calls,
        writes=writes, saves=saves, audio=audio, photos=photos, sent=sent, committed=committed,
        snapshot=snapshot, emotion=emotion, user_port=user_port, private=private)


def run_contact(env):
    from runtime.personal_chat import backend
    event = PersonalMessage('qq', 'bot', 'owner', 'contact', '')
    async def scenario():
        await env.service.proactive(event, env.sender, eligible=lambda:backend._contact_eligible(env.server,event))
        await asyncio.sleep(0)
    asyncio.run(scenario())


@pytest.mark.parametrize('medium', ['text', 'audio_speech'])
def test_real_pipeline_freezes_contact_before_writer_and_obeys_medium(monkeypatch, tmp_path, medium):
    env = environment(monkeypatch, tmp_path, medium=medium)
    run_contact(env)
    assert len(env.calls) == len(env.writes) == len(env.sent) == 1
    assert env.user_port.turns == []  # Proactive never becomes a fabricated received user turn.
    assert env.calls[0]['activity'] == {key: env.snapshot['current'][key]
        for key in ('occurred_at', 'location', 'activity', 'note')}
    assert 'current' not in env.calls[0]['world']  # Identity remains in the host freshness gate.
    assert env.calls[0]['world']['schedule'] == env.snapshot['world']['schedule']
    assert env.calls[0]['rhythm'] == env.snapshot['rhythm']
    assert env.calls[0]['emotion']['current_affect'] == env.emotion['current_affect']
    assert '<proactive_decision>' in str(env.writes[0])
    assert 'connection' in str(env.writes[0])
    assert not env.photos and not env.rows[0].get('letter_invitation')
    assert env.sent[0][0] == ('audio' if medium == 'audio_speech' else 'text')
    assert env.audio == (['读到一句很喜欢的话，想跟你说说'] if medium == 'audio_speech' else [])
    assert env.rows[0]['delivery_status'] == 'DELIVERED'


def test_real_pipeline_defer_is_persisted_without_writer_or_send(monkeypatch, tmp_path):
    env = environment(monkeypatch, tmp_path, action='defer')
    run_contact(env)
    assert len(env.calls) == 1 and not env.writes and not env.sent and not env.audio
    assert not env.photos and not env.committed and not env.user_port.turns
    assert env.rows[0]['proactive_decision']['decision']['action'] == 'defer'
    assert env.rows[0]['delivery_status'] == 'SKIPPED'


@pytest.mark.parametrize('chosen', [21, 24])
def test_proactive_keeps_live_state_when_selector_skips_it(monkeypatch, tmp_path, chosen):
    """Replay the two recorded selection maps with synthetic local facts."""
    from runtime.reply import jev_questions
    env = environment(monkeypatch, tmp_path)
    packet = dict(base=dict(kind='character_life_reference', stale=False, current=None,
                           as_of=env.snapshot['current']['occurred_at']),
                  rhythm=env.snapshot['rhythm'], records=[
                      dict(field='schedule', value=env.snapshot['world']['schedule']),
                      dict(field='weather', value={}),
                      dict(field='character_development', value={}),
                      dict(field='current', value=env.snapshot['current']),
                      *[dict(field='threads', many=True, value=dict(id=f't{i}', title='一件小事'))
                        for i in range(27)]])
    class Selector:
        async def ask(self, state, questions, *, purpose):
            assert purpose == 'reply-world-selection'
            return {key: 'useful' if key == f'r{chosen}' else 'skip' for key in questions}
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Selector())
    env.server.letters_adapter.daily_life = SimpleNamespace(store=SimpleNamespace(
        reply_candidates=lambda **kwargs: deepcopy(packet), addressing_profile=lambda **kwargs: []))
    env.server.letters_adapter.recent_letter_fragments = lambda *args, **kwargs: ()
    run_contact(env)
    assert len(env.calls) == len(env.writes) == len(env.sent) == 1
    assert env.calls[0]['activity']['occurred_at'] == env.snapshot['current']['occurred_at']
    assert env.calls[0]['activity']['note'] == env.snapshot['current']['note']
    assert env.calls[0]['world']['schedule'] == env.snapshot['world']['schedule']
    assert env.rows[0]['delivery_status'] == 'DELIVERED'


def test_writer_cannot_reverse_send_decision_into_skip(monkeypatch, tmp_path):
    from tests.http.test_personal_chat_decision import envelope
    env = environment(monkeypatch, tmp_path, body=envelope(text='', skip=True))
    with pytest.raises(RuntimeError, match='JEV_PLAN_UNSUPPORTED'):
        run_contact(env)
    assert not env.sent and not env.audio
    assert env.rows[0]['proactive_decision']['decision']['action'] == 'send'


def test_world_changed_during_persona_or_emotion_await_cannot_authorize_old_view(monkeypatch, tmp_path):
    env = environment(monkeypatch, tmp_path)
    adapter = env.server.letters_adapter
    async def appraise(*a, **k):
        env.snapshot['current']['source_id'] = 'newer-world-event'
        return env.emotion
    adapter.prepare_character_emotion = appraise
    with pytest.raises(RuntimeError, match='JEV_CONTEXT_UNAVAILABLE'):
        run_contact(env)
    assert not env.calls and not env.writes and not env.sent


@pytest.mark.parametrize('change', ['class', 'activity', 'relationship', 'pause'])
def test_live_state_change_after_decision_cancels_generated_contact(monkeypatch, tmp_path, change):
    def alter(snapshot):
        if change == 'class': snapshot['world']['schedule']['current_class'] = {'name':'music history'}
        elif change == 'activity': snapshot['current']['source_id'] = 'day:new'
        elif change == 'relationship': env.private.tension = 80
        else: env.server.store.letters.append(dict(letter_id='pause', origin='user', letter_status='COMPLETED',
            created_at=1, initiative_preference='pause', pause_until=None))
    env = environment(monkeypatch, tmp_path, writer_hook=alter)
    run_contact(env)
    assert len(env.calls) == len(env.writes) == 1 and not env.sent
    assert env.rows[0]['delivery_status'] == 'SKIPPED'
    assert env.rows[0]['error_code'] == 'PERSONAL_CHAT_CONTACT_SUPERSEDED'


@pytest.mark.parametrize('failure', ['port', 'save', 'audio_upload'])
def test_contact_errors_do_not_replan_or_silently_change_delivery(monkeypatch, tmp_path, failure):
    from runtime.reply import companion_proactive
    from runtime.reply.companion_decision import CompanionDecisionError
    env = environment(monkeypatch, tmp_path, medium='audio_speech')
    if failure == 'port':
        def broken(encoded): raise CompanionDecisionError('JEV_UNAVAILABLE')
        monkeypatch.setattr(companion_proactive.configured_proactive(), '_request', broken)
        code = 'JEV_UNAVAILABLE'
    elif failure == 'save':
        original = env.server._persist_store_state
        def persist():
            if env.rows and env.rows[0].get('proactive_decision'):
                raise OSError('synthetic persistence failure')
            original()
        env.server._persist_store_state = persist
        code = 'JEV_DECISION_NOT_SAVED'
    else:
        async def broken(path): raise RuntimeError('synthetic audio upload failure')
        env.sender.prepare_audio = broken
        code = 'JEV_PLAN_UNSUPPORTED'
    with pytest.raises(RuntimeError, match=code):
        run_contact(env)
    assert not env.sent and not env.photos and not env.user_port.turns
    assert env.rows[0]['delivery_status'] == 'FAILED'
    if failure != 'audio_upload': assert not env.writes


@pytest.mark.parametrize('gate', ['native_slot', 'busy', 'channel_changed', 'free'])
def test_runtime_tick_holds_shared_contact_slot_and_rechecks_channel(monkeypatch, tmp_path, gate):
    from runtime.personal_chat import backend
    from runtime.personal_chat.initiative import Initiative
    from runtime.reply import proactive_runtime
    runtime = {'status':{'qq':'CONNECTED'}}
    env = environment(monkeypatch, tmp_path,
        writer_hook=(lambda _:runtime['status'].update(qq='RECONNECTING')) if gate == 'channel_changed' else None)
    runtime['service'] = env.service
    monkeypatch.setattr(backend, 'selected_channels', lambda _: {'qq'})
    initiative = Initiative(env.rows, clock=lambda:datetime(2026,9,27,12,tzinfo=timezone.utc).timestamp(),
        interval=lambda:0, profile_provider=lambda:proactive_runtime.live_profile(env.server))
    initiative.received(PersonalMessage('qq','bot','owner','prior','此前收到的原话'), env.sender)
    if gate == 'busy': env.snapshot['current']['activity_kind'] = 'practice'
    async def scenario():
        if gate == 'native_slot':
            with proactive_runtime.contact_slot(env.server, 'letter') as acquired:
                assert acquired
                await backend._proactive_contact(env.server, runtime, initiative)
                assert env.server._jev_contact_channel == 'letter'
        else:
            await backend._proactive_contact(env.server, runtime, initiative)
        await asyncio.sleep(0)
    asyncio.run(scenario())
    assert env.server._jev_contact_channel is None
    if gate == 'free':
        assert len(env.calls) == len(env.sent) == 1
    else:
        assert not env.sent
        assert len(env.calls) == (1 if gate == 'channel_changed' else 0)


@pytest.mark.parametrize('changed_during', ['generation', 'audio_upload'])
def test_proactive_rechecks_live_eligibility_before_platform_send(changed_during):
    async def scenario():
        rows, sent, allowed = [], [], [True]
        async def generate(event, row):
            if changed_during == 'generation':
                allowed[0] = False
            else:
                row['prepared_audio'] = 'synthetic.wav'
            return '刚才听到一段很有意思的旋律'
        class Sender:
            async def __call__(self, text): sent.append(text)
            async def audio(self, path): sent.append(path)
            async def prepare_audio(self, path):
                allowed[0] = False
                return path
        async def commit(row): raise AssertionError('blocked contact cannot commit')
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        await service.proactive(PersonalMessage('qq', 'bot', 'owner', 'check', ''), Sender(),
                                eligible=lambda: allowed[0])
        assert sent == []
        assert rows[0]['delivery_status'] == 'SKIPPED'
        assert rows[0]['error_code'] == 'PERSONAL_CHAT_CONTACT_SUPERSEDED'
    asyncio.run(scenario())


def test_proactive_unknown_delivery_is_never_replayed():
    async def scenario():
        rows, sent = [], []
        async def generate(event, row): return '刚才路过琴房想起上次那段旋律'
        async def send(text):
            sent.append(text)
            raise RuntimeError('connection lost after transmission')
        async def commit(row): raise AssertionError('uncertain contact cannot commit')
        event = PersonalMessage('qq', 'bot', 'owner', 'check', '')
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        with pytest.raises(RuntimeError, match='connection lost'):
            await service.proactive(event, send)
        restarted = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        with pytest.raises(RuntimeError, match='DELIVERY_REQUIRES_ATTENTION'):
            await restarted.proactive(event, send)
        assert len(sent) == 1 and rows[0]['delivery_status'] == 'SENDING'
    asyncio.run(scenario())


def test_user_intake_cancels_pending_proactive_decision():
    async def scenario():
        rows, sent, started = [], [], asyncio.Event()
        async def generate(event, row):
            started.set()
            await asyncio.Event().wait()
        async def send(text): sent.append(text)
        async def commit(row): raise AssertionError('cancelled contact cannot commit')
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        task = asyncio.create_task(service.proactive(PersonalMessage('qq', 'bot', 'owner', 'check', ''), send))
        await started.wait()
        await service.ingest(PersonalMessage('qq', 'bot', 'owner', 'new', '我回来了'))
        await task
        assert sent == []
        assert rows[0]['delivery_status'] == 'SKIPPED'
        assert rows[0]['error_code'] == 'PERSONAL_CHAT_USER_PRIORITY'
        assert rows[1]['content'] == '我回来了' and rows[1]['delivery_status'] == 'RECEIVED'
    asyncio.run(scenario())
