import json
from datetime import datetime, timedelta
import pytest

from runtime.personal_chat.decision import decode
from runtime.personal_chat.initiative import Initiative
from runtime.private_world.life_rhythm import LOCAL


def envelope(**values):
    body = dict(text='好呀', delivery='text', listening='keep', initiative='keep', pause_until=None,
                letter='keep', letter_until=None, followup_at=None, evidence='', sticker=None, skip=False)
    return json.dumps(body | values, ensure_ascii=False)


@pytest.mark.parametrize('variant', ['fenced', 'missing_sticker', 'numeric_sticker', 'extra_fields'])
def test_harmless_envelope_variations_preserve_reply_without_changing_preferences(variant):
    body = json.loads(envelope(text='今天练得挺顺，休息一下。'))
    if variant == 'missing_sticker':
        body.pop('sticker')
    elif variant == 'numeric_sticker':
        body['sticker'] = 1
    elif variant == 'extra_fields':
        body.update(reason='普通聊天', followup_cancel=True, initiative_preference='open')
    raw = json.dumps(body)
    if variant == 'fenced':
        raw = '```json\n' + raw + '\n```'
    result = decode(raw, user='今天钢琴练得怎么样？', now=1)
    assert result['text'] == body['text']
    assert result['sticker'] is None
    assert result['initiative'] == result['letter'] == result['listening'] == 'keep'
    assert result['followup_at'] is None and result['followup_cancel'] is False
    assert 'reason' not in result and 'initiative_preference' not in result


@pytest.mark.parametrize('raw', [
    '说明\n```json\n' + envelope() + '\n```',
    '```json\n' + envelope() + '\n```\n其他文字',
    envelope().replace('"initiative": "keep", ', ''),
])
def test_envelope_tolerance_does_not_guess_controls_or_extract_partial_json(raw):
    with pytest.raises(ValueError, match='PERSONAL_CHAT_DECISION_INVALID'):
        decode(raw, user='你好', now=1)


@pytest.mark.parametrize('marker', ['[QQ表情]', '(QQ表情)', '（QQ表情）', '【QQ表情】'])
@pytest.mark.parametrize('delivery', ['text', 'voice'])
def test_incoming_face_placeholder_cannot_leak_into_reply_or_speech(marker, delivery):
    decision = decode(envelope(text='那画面也太可爱了吧' + marker + '，哈哈🙂', delivery=delivery),
                      user='小猫在踩奶[QQ表情]', now=1)
    assert decision['text'] == '那画面也太可爱了吧，哈哈🙂'


def test_placeholder_only_reply_is_rejected_instead_of_sending_empty_text():
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(envelope(text='[QQ表情]'), user='[QQ表情]', now=1)


def test_explicit_followup_persists_deadline_and_temporary_pause_expires():
    now = datetime(2026, 9, 13, 12, tzinfo=LOCAL)
    later = now + timedelta(days=1)
    text = '今晚别找我，明天中午再来找我'
    decision = decode(envelope(initiative='pause', followup_at=later.isoformat(), evidence=text), user=text, now=now.timestamp())
    assert decision['pause_until'] == decision['followup_at'] == later.timestamp()
    clock = [now.timestamp()]
    row = dict(letter_id='request', delivery_status='DELIVERED', initiative_preference='pause',
               pause_until=decision['pause_until'], followup_at=decision['followup_at'], followup_quote=text)
    rows = [row]
    policy = Initiative(rows, clock=lambda: clock[0], interval=lambda: 3600)
    policy.received(None, None)
    assert not policy.ready()
    clock[0] = later.timestamp()
    assert policy.ready() and policy.pending_followup() == row
    rows.append(dict(origin='proactive', delivery_status='SENDING', followup_source_id='request'))
    assert policy.pending_followup() is None and not policy.ready()


@pytest.mark.parametrize('raw', ['好呀', '{"text":"好呀"}', envelope(text='好呀[[chat:text|keep'),
    envelope(skip=True)])
def test_missing_invalid_or_unsupported_decision_cannot_be_sent(raw):
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(raw, user='明天找我', now=datetime(2026,9,13,12,tzinfo=LOCAL).timestamp())


@pytest.mark.parametrize('kind,user', [
    ('wait_user', '我还没说完，先等我说完'),
    ('no_reply', '这一条不用回复我'),
])
def test_explicit_user_silence_requires_current_quote_and_exposes_finite_kind(kind, user):
    decision = decode(envelope(text='', skip=True, silence=dict(kind=kind, evidence=user)),
                      user=user, now=1, allow_user_silence=True)
    assert decision['skip'] and decision['text'] == ''
    assert decision['silence_kind'] == kind
    assert decision['silence'] == dict(kind=kind, evidence=user)
    assert decision['initiative'] == 'keep' and not decision['followup_cancel']


def test_user_silence_still_requires_explicit_decoder_opt_in():
    user = '先等我说完'
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(envelope(text='', skip=True, silence=dict(kind='wait_user', evidence=user)),
               user=user, now=1)


@pytest.mark.parametrize('silence', [
    None, [], 'no_reply', {},
    dict(kind='defer', evidence='先等我说完'),
    dict(kind=True, evidence='先等我说完'),
    dict(kind='no_reply', evidence=''),
    dict(kind='no_reply', evidence='  \n'),
    dict(kind='no_reply', evidence=1),
    dict(kind='no_reply', evidence='上一轮说不用回复'),
    dict(kind='no_reply', evidence='先等我说完', due_at='2026-10-04T12:00:00+08:00'),
    dict(kind='wait_user'),
    dict(evidence='先等我说完'),
])
def test_malformed_or_unbound_user_silence_cannot_terminate_turn(silence):
    with pytest.raises(ValueError, match='DECISION_INVALID') as error:
        decode(envelope(text='', skip=True, silence=silence),
               user='先等我说完', now=1, allow_user_silence=True)
    assert error.value.reason == 'SILENCE_INVALID'


def test_skip_without_silence_remains_invalid_even_when_decoder_opts_in():
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(envelope(text='', skip=True), user='今天累得有点难受', now=1,
               allow_user_silence=True)


@pytest.mark.parametrize('skip,text', [(False, '好，我等你'), (True, '好，我等你')])
def test_silence_cannot_accompany_reply_text_or_non_skip(skip, text):
    user = '先等我说完'
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(envelope(text=text, skip=skip, silence=dict(kind='wait_user', evidence=user)),
               user=user, now=1, allow_user_silence=True)


def test_non_skip_cannot_carry_null_silence():
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(envelope(silence=None), user='今天怎么样', now=1, allow_user_silence=True)


def test_normal_reply_has_no_silence_disposition():
    assert decode(envelope(), user='你好', now=1, allow_user_silence=True)['silence_kind'] is None


def test_proactive_skip_keeps_existing_contract_and_cannot_authorize_user_controls():
    user = '不用回复，也别主动找我'
    decision = decode(envelope(text='', skip=True, initiative='pause', evidence=user),
                      user=user, now=1, proactive=True, allow_user_silence=True)
    assert decision['skip'] and decision['silence_kind'] is None
    assert decision['initiative'] == 'keep'
    assert decision['dropped_controls'] == 'UNSUPPORTED_PREFERENCE_CHANGE'
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(envelope(text='', skip=True, initiative='pause', evidence=user,
                        silence=dict(kind='no_reply', evidence=user)),
               user=user, now=1, proactive=True, allow_user_silence=True)


def test_valid_user_silence_preserves_separately_validated_pause_and_cancellation():
    user = '这一条不用回复我，也别主动找我，之前的约定取消'
    decision = decode(envelope(text='', skip=True, initiative='pause', followup_at='cancel',
                        evidence='别主动找我，之前的约定取消',
                        silence=dict(kind='no_reply', evidence='这一条不用回复我')),
                      user=user, now=1, allow_user_silence=True)
    assert decision['silence_kind'] == 'no_reply'
    assert decision['initiative'] == 'pause' and decision['pause_until'] is None
    assert decision['followup_cancel'] and decision['followup_at'] is None


@pytest.mark.parametrize('controls,reason', [
    (dict(initiative='pause', evidence='伪造原话'), 'UNSUPPORTED_PREFERENCE_CHANGE'),
    (dict(followup_at='2030-01-01T12:00:00+09:00', evidence='不用回复我'), 'TIME_RANGE'),
])
def test_user_silence_does_not_bypass_control_evidence_or_time_limits(controls, reason):
    user = '不用回复我'
    decision = decode(envelope(text='', skip=True, silence=dict(kind='no_reply', evidence=user), **controls),
                      user=user, now=datetime(2026,10,3,12,tzinfo=LOCAL).timestamp(),
                      allow_user_silence=True)
    assert decision['silence_kind'] == 'no_reply'
    assert decision['dropped_controls'] == reason
    assert decision['initiative'] == 'keep' and decision['followup_at'] is None
    assert not decision['followup_cancel']


@pytest.mark.parametrize('raw,reason', [
    ('```json\n' + envelope(initiative='open', evidence='可以主动找我') + '\n```', 'UNSUPPORTED_PREFERENCE_CHANGE'),
    (envelope(initiative='pause', evidence='伪造原话'), 'UNSUPPORTED_PREFERENCE_CHANGE'),
    (envelope(followup_at='2030-01-01T12:00:00+09:00', evidence='明天找我'), 'TIME_RANGE'),
])
def test_invalid_controls_are_dropped_and_the_reply_is_still_sent(raw, reason):
    decision = decode(raw, user='明天找我', now=datetime(2026,9,13,12,tzinfo=LOCAL).timestamp())
    assert decision['text'] == '好呀'
    assert decision['dropped_controls'] == reason
    assert decision['initiative'] == decision['letter'] == decision['listening'] == 'keep'
    assert decision['followup_at'] is None and decision['pause_until'] is None and not decision['followup_cancel']


def test_requested_early_morning_followup_is_kept():
    now = datetime(2026,9,13,23,tzinfo=LOCAL).timestamp()
    decision = decode(envelope(followup_at='2026-09-14T07:00:00+08:00', evidence='明早七点叫我起床'),
                      user='明早七点叫我起床', now=now)
    assert decision['followup_at'] == datetime(2026,9,14,7,tzinfo=LOCAL).timestamp()
    assert 'dropped_controls' not in decision


def test_proactive_cannot_modify_user_preferences_and_cancel_survives_restart():
    now = datetime(2026,9,13,12,tzinfo=LOCAL).timestamp()
    ignored = decode(envelope(initiative='open', evidence='可以'), user='可以', now=now, proactive=True)
    assert ignored['initiative'] == 'keep' and ignored['dropped_controls'] == 'UNSUPPORTED_PREFERENCE_CHANGE'
    rows = [dict(letter_id='request', delivery_status='DELIVERED', followup_at=now + 100),
            dict(letter_id='cancel', delivery_status='DELIVERED', followup_at=None, initiative_preference='pause')]
    assert Initiative(json.loads(json.dumps(rows)), clock=lambda: now).pending_followup() is None
    decision = decode(envelope(followup_at='cancel', evidence='明天的约定取消吧'),
                      user='明天的约定取消吧', now=now)
    assert decision['followup_cancel'] and decision['initiative'] == 'keep'


def test_chat_channel_is_kept_in_shared_recent_history():
    from runtime.reply.recent_correspondence import recent_correspondence
    row = dict(letter_id='im', reply_revision=1, letter_status='COMPLETED',
               private_world_occurred_at='2026-09-13T12:00:00+00:00',
               content='地铁好挤', reply_text='鞋还在就好。', channel='wechat', reply_mode='future_im')
    projected = json.loads(recent_correspondence([row]))['letters'][0]
    assert projected['channel'] == 'wechat' and projected['message_kind'] == 'instant_chat'
    assert '不是一封信' in projected['source_note']


def test_retry_discards_unpublished_audio_and_decisions():
    import asyncio
    from runtime.personal_chat.events import PersonalMessage
    from runtime.personal_chat.service import PersonalChatService
    async def run():
        rows, output = [], []
        async def generate(event, row):
            if row['generation_attempts'] == 1:
                row.update(prepared_audio='unpublished.wav', initiative_preference='pause', followup_at=123,
                           delivery_basis='SPEAKER_UNAVAILABLE', voice_ready=True)
                raise RuntimeError('failed')
            return '新的文字回复'
        async def send(text):
            output.append(text)
        async def audio(path):
            raise AssertionError('stale audio must not be sent')
        async def commit(row):
            pass
        send.audio = audio
        service = PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('bot', 'owner')})
        event = PersonalMessage('qq', 'bot', 'owner', '1', 'hi')
        with pytest.raises(RuntimeError):
            await service.handle(event, send)
        await service.handle(event, send)
        assert output == ['新的文字回复']
        assert 'initiative_preference' not in rows[0] and 'followup_at' not in rows[0]
        assert 'delivery_basis' not in rows[0] and 'voice_ready' not in rows[0]
    asyncio.run(run())


def test_failed_new_user_request_suppresses_initiative_until_next_completed_exchange():
    now = datetime(2026,9,13,12,tzinfo=LOCAL).timestamp()
    rows = [dict(delivery_status='DELIVERED'), dict(delivery_status='FAILED')]
    policy = Initiative(rows, clock=lambda: now, interval=lambda: 0)
    policy.received(None, None)
    assert not policy.ready()
    rows.append(dict(delivery_status='DELIVERED'))
    assert policy.ready()


def test_requested_wake_up_call_is_kept_during_quiet_hours():
    # Her own initiative waits for 08:30; an appointment the user asked for does not.
    due = datetime(2026, 9, 14, 7, tzinfo=LOCAL).timestamp()
    rows = [dict(letter_id='request', delivery_status='DELIVERED', followup_at=due,
                 followup_quote='明早七点叫我起床')]
    policy = Initiative(rows, clock=lambda: due + 60, interval=lambda: 3600)
    policy.received(None, None)
    assert policy.pending_followup() == rows[0] and policy.ready()
    unscheduled = Initiative([], clock=lambda: due + 60, interval=lambda: 0)
    unscheduled.received(None, None)
    assert not unscheduled.ready()


def test_due_appointment_waives_world_gates_only():
    from runtime.reply import proactive_runtime
    from types import SimpleNamespace
    snapshot = {'rhythm': {'phase': 'sleep'}, 'world': {}}
    server = SimpleNamespace(daily_life_runtime=SimpleNamespace(store=SimpleNamespace(snapshot=lambda now: snapshot)),
                             store=SimpleNamespace(letters=[], personal_chats=[]))
    now = datetime(2026, 9, 14, 7, tzinfo=LOCAL)
    original = proactive_runtime.live_profile
    proactive_runtime.live_profile = lambda server: SimpleNamespace(im_interval_min=0, im_interval_max=0)
    original_gates = proactive_runtime.contact_gates
    proactive_runtime.contact_gates = lambda rows, **k: {'paused': False, 'blocked_reasons': ['pending_reply']}
    try:
        asleep = proactive_runtime.live_state(server, channel='qq', now=now)['gates']['blocked_reasons']
        due = proactive_runtime.live_state(server, channel='qq', now=now, appointment_due=True)['gates']['blocked_reasons']
    finally:
        proactive_runtime.live_profile, proactive_runtime.contact_gates = original, original_gates
    assert asleep == ['sleeping', 'pending_reply']
    assert due == ['pending_reply']


def test_misplaced_illustration_marker_is_stripped_not_fatal():
    decision = decode(envelope(text='[[image:linli-40]]' + chr(10) + '快去睡觉。'), user='晚安', now=1)
    assert decision['text'] == '快去睡觉。'
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(envelope(text='好呀[[chat:text|keep]]'), user='晚安', now=1)
