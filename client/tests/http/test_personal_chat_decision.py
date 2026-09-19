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
    envelope(initiative='pause', evidence='伪造原话'), envelope(skip=True),
    envelope(followup_at='2030-01-01T12:00:00+09:00', evidence='明天找我')])
def test_missing_invalid_or_unsupported_decision_cannot_be_sent(raw):
    with pytest.raises(ValueError, match='DECISION_INVALID'):
        decode(raw, user='明天找我', now=datetime(2026,9,13,12,tzinfo=LOCAL).timestamp())


def test_proactive_cannot_modify_user_preferences_and_cancel_survives_restart():
    now = datetime(2026,9,13,12,tzinfo=LOCAL).timestamp()
    with pytest.raises(ValueError):
        decode(envelope(initiative='open', evidence='可以'), user='可以', now=now, proactive=True)
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


def test_personal_json_keeps_high_reasoning_and_ten_thousand_cap():
    from llm_gateway import GatewayConfig, GatewayRequestScope, OpenAICompatibleAdapter
    gateway = OpenAICompatibleAdapter(GatewayConfig(provider='openai_compatible',
        base_url='https://api.deepseek.com/v1', model='deepseek-flash'))
    scope = GatewayRequestScope.PERSONAL_CHAT_JSON
    body = gateway._body([{'role':'user','content':'Return JSON'}], stream=False,
                         max_reasoning=gateway._uses_max_reasoning(scope), scope=scope)
    assert body['response_format'] == {'type':'json_object'}
    assert body['reasoning_effort'] == 'high' and body['max_tokens'] == 10000
    assert not gateway._uses_official_review_responses(scope)


def test_retry_discards_unpublished_audio_and_decisions():
    import asyncio
    from runtime.personal_chat.events import PersonalMessage
    from runtime.personal_chat.service import PersonalChatService
    async def run():
        rows, output = [], []
        async def generate(event, row):
            if row['generation_attempts'] == 1:
                row.update(prepared_audio='unpublished.wav', initiative_preference='pause', followup_at=123)
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
    asyncio.run(run())


def test_failed_new_user_request_suppresses_initiative_until_next_completed_exchange():
    now = datetime(2026,9,13,12,tzinfo=LOCAL).timestamp()
    rows = [dict(delivery_status='DELIVERED'), dict(delivery_status='FAILED')]
    policy = Initiative(rows, clock=lambda: now, interval=lambda: 0)
    policy.received(None, None)
    assert not policy.ready()
    rows.append(dict(delivery_status='DELIVERED'))
    assert policy.ready()
