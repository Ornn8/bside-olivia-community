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


@pytest.mark.parametrize('raw', ['好呀', '{}', '{"text":null}', envelope(text='好呀[[chat:text|keep'),
    envelope(skip=True)])
def test_missing_invalid_or_unsupported_decision_cannot_be_sent(raw):
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(raw, user='明天找我', now=datetime(2026,9,13,12,tzinfo=LOCAL).timestamp())


@pytest.mark.parametrize('missing', ['delivery', 'listening', 'initiative', 'pause_until', 'letter',
    'letter_until', 'followup_at', 'evidence', 'skip', 'all_metadata'])
def test_missing_metadata_preserves_body_with_neutral_non_executing_defaults(missing):
    body = json.loads(envelope(text='当然可以，我在听你说。'))
    if missing == 'all_metadata':
        body = {'text': body['text']}
    else:
        body.pop(missing)
    result = decode(json.dumps(body), user='陪我说会话', now=1)
    assert result['text'] == '当然可以，我在听你说。'
    assert result['skip'] is False
    assert result['listening'] == result['initiative'] == result['letter'] == 'keep'
    assert result['pause_until'] is result['letter_until'] is result['followup_at'] is None
    assert missing in result['defaulted_fields'] or missing == 'all_metadata'


@pytest.mark.parametrize('body', [
    {'text': ''}, {'text': 42}, {'text': '你好', 'skip': 'false'},
    {'text': '', 'skip': True}, {'text': '你好', 'delivery': 'private-command'},
])
def test_metadata_defaults_do_not_allow_empty_reply_unproven_silence_or_invalid_media(body):
    with pytest.raises(ValueError, match='PERSONAL_CHAT_DECISION_INVALID'):
        decode(json.dumps(body), user='你好', now=1)


@pytest.mark.parametrize('missing', ['pause_until', 'letter_until', 'initiative', 'evidence'])
def test_incomplete_control_metadata_cannot_create_indefinite_pause(missing):
    body = json.loads(envelope(text='好，今天不催你写信。', letter='pause',
        letter_until='2026-10-05T00:00:00+08:00', evidence='今天不想写信'))
    body.pop(missing)
    result = decode(json.dumps(body), user='今天不想写信',
        now=datetime(2026, 10, 4, 12, tzinfo=LOCAL).timestamp())
    assert result['text'] == '好，今天不催你写信。'
    assert result['letter'] == result['initiative'] == 'keep'
    assert result['letter_until'] is result['pause_until'] is None
    assert result['dropped_controls'] == 'INCOMPLETE_CONTROLS'


@pytest.mark.parametrize('text', [
    '列表可以这样写：[[1, 2], [3, 4]]。',
    '笔记里可用 [[项目页面]] 链接到另一个页面。',
    '括号里的内容是 [a[b]]，不是命令。',
    '这里的两个字符 ]] 只是闭合括号。',
])
def test_ordinary_brackets_are_preserved(text):
    assert decode(envelope(text=text), user='解释下这个格式', now=1)['text'] == text


@pytest.mark.parametrize('marker', [
    '[[chat:text|keep]]', '[[chat:text|keep', '[[delivery:voice|keep]]',
    '[[initiative:pause]]', '[[letter:invite]]', '[[skip]]', '[[CONTROL:private]]',
])
def test_reserved_control_syntax_remains_rejected(marker):
    with pytest.raises(ValueError, match='PERSONAL_CHAT_DECISION_INVALID'):
        decode(envelope(text='这段正文'+marker), user='你好', now=1)


@pytest.mark.parametrize('script', [
    {'unexpected': 'optional metadata'},
    dict(title='小故事', spoken_text='这是一个虚构的小故事。'*10, continuation_summary='虚构摘要'),
])
def test_unrequested_speech_is_ignored_without_losing_reply(script):
    result = decode(envelope(text='好，我在听你说。', speech=script), user='陪我说话',
                    now=1, allow_speech=False)
    assert result['text'] == '好，我在听你说。' and result['speech'] is None
    assert result['dropped_media'] == 'UNREQUESTED_SPEECH'


def test_requested_invalid_speech_is_not_delivered_or_accepted_as_valid_script():
    with pytest.raises(ValueError, match='PERSONAL_CHAT_DECISION_INVALID'):
        decode(envelope(text='给你讲个小故事。', speech={'unexpected': 'invalid'}),
               user='给我讲故事', now=1, allow_speech=True)


@pytest.mark.parametrize('invalid', [
    {'listening': 'unknown'}, {'initiative': 'erase'}, {'letter': []},
    {'evidence': None}, {'evidence': 42}, {'letter_invitation': 'yes'},
])
def test_invalid_optional_control_shape_drops_side_effects_preserves_body(invalid):
    body = json.loads(envelope(text='好的，先忙你的。', initiative='pause',
                               followup_at='cancel', evidence='先忙我的'))
    body.update(invalid)
    result = decode(json.dumps(body), user='先忙我的', now=1)
    assert result['text'] == '好的，先忙你的。'
    assert result['listening'] == result['initiative'] == result['letter'] == 'keep'
    assert result['followup_at'] is result['pause_until'] is result['letter_until'] is None
    assert result['followup_cancel'] is False and result['letter_invitation'] is False
    assert result['dropped_controls'] == 'CONTROL_SHAPE_INVALID'


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


@pytest.mark.parametrize('text', ['[[image:linli-40]]', '[[sticker:linli-01]]', '[picture:linli-02]'])
def test_presentation_marker_alone_is_not_an_empty_success(text):
    with pytest.raises(ValueError, match='PERSONAL_CHAT_DECISION_INVALID') as caught:
        decode(envelope(text=text), user='你好', now=1)
    assert caught.value.reason == 'EMPTY_OR_SKIPPED_REPLY'


@pytest.mark.parametrize('extra', [False, True])
def test_rejection_diagnostic_counts_only_original_unknown_annotations(extra):
    body = json.loads(envelope(text='好呀', skip='false'))
    if extra:
        body['annotation'] = 'private explanation'
    with pytest.raises(ValueError, match='PERSONAL_CHAT_DECISION_INVALID') as caught:
        decode(json.dumps(body), user='你好', now=1)
    assert caught.value.extra_field_count == int(extra)
    assert 'private' not in str(caught.value) and not caught.value.missing_fields
