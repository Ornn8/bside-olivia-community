from datetime import datetime
import pytest

from runtime.personal_chat.engagement import BEIJING


def at(hour, minute=0, day=16):
    return datetime(2026, 10, day, hour, minute, tzinfo=BEIJING).timestamp()


def test_daily_window_and_success_across_channels():
    from runtime.reply.daily_proactive import daily_candidate
    assert daily_candidate([], at(19))
    for hour, minute in [(0, 0), (8, 29), (18, 59), (22, 0)]:
        assert daily_candidate([], at(hour, minute)) is None
    for row in [dict(channel='qq', delivery_status='DELIVERED'), dict(letter_status='COMPLETED')]:
        assert daily_candidate([dict(row, origin='proactive', created_at=at(10))], at(19)) is None
        assert daily_candidate([dict(row, origin='proactive', created_at=at(20, day=15))], at(19))


def test_legacy_false_is_on_but_other_settings_survive(tmp_path):
    from runtime.reply.proactive_letters import load_settings, write_json
    write_json(tmp_path / 'proactive/settings.json', {'enabled': False, 'allow_voice': False})
    assert load_settings(tmp_path) == dict(enabled=True, allow_voice=True, login_check_enabled=False)
    write_json(tmp_path / 'proactive/settings.json', {'enabled': False, 'allow_voice': False})
    assert load_settings(tmp_path)['enabled'] is True


def test_reserved_has_daily_opportunity_without_history():
    from runtime.reply.proactive_letters import make_context
    context = make_context([], now=at(19))
    assert context['candidates'][0]['kind'] == 'daily_checkin'


def test_daily_policy_preserves_hard_gates():
    import asyncio
    from runtime.reply.proactive_runtime import decide
    from tests.persona.test_companion_proactive import packet
    value = packet()
    value['opportunities'] = [dict(id='daily:2026-10-16', kind='connection', description='Evening greeting')]
    result = asyncio.run(decide(value))
    assert result.decision['action'] == 'send'
    assert result.response_digest is None  # Application policy, never a fabricated JEV receipt.
    for gates in [dict(paused=True, blocked_reasons=[]), dict(paused=False, blocked_reasons=['pending_reply'])]:
        value['hard_gates'] = gates
        assert asyncio.run(decide(value)).decision['action'] == 'defer'


def test_daily_retries_and_uncertain_delivery():
    from runtime.reply.daily_proactive import daily_candidate, daily_retry_ids
    assert daily_candidate([dict(origin='proactive', channel='qq', delivery_status='DELIVERY_UNCONFIRMED',
                                 created_at=at(19, day=15))], at(20)) is None
    attempts = [dict(id='daily:2026-10-16', at=at(19))]
    assert daily_retry_ids(attempts, at(19, 29)) == {'daily:2026-10-16'}
    assert daily_retry_ids(attempts, at(19, 30)) == set()
    assert daily_retry_ids(attempts * 3, at(21)) == {'daily:2026-10-16'}


@pytest.mark.parametrize('jev', [False, True])
def test_daily_letter_publishes_once_without_history(tmp_path, jev):
    from tests.http.test_expression_context_routes import run_isolated
    from tests.http.test_proactive_expression_context import SETUP
    run_isolated(tmp_path, SETUP + '\njev = ' + repr(jev) + r'''
if jev:
    import os
    from runtime.reply import companion_duties
    from runtime.reply.companion_duties import FrozenCompanionDutyResult
    os.environ['OLIVIA_JEV_DECISION_URL'] = 'http://127.0.0.1:8097/v1/companion/decide'
    class PersonaProbe:
        async def evaluate(self, kind, value):
            assert kind == 'persona'
            return FrozenCompanionDutyResult(decision_json='{"persona_ids":[]}',
                input_digest=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest())
    companion_duties.configured_duties = lambda: PersonaProbe()
NOW = datetime(2026, 10, 16, 11, tzinfo=timezone.utc)
server.store.letters.clear()
server.store.personal_chats.clear()
snapshot = {'rhythm': {'availability':'open', 'phase':'free'}, 'world':{'schedule':{}}, 'stale':True}
server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(
    snapshot=lambda now: snapshot, exchange_state=lambda: snapshot))
server._refresh_proactive_context()
intent = scan_pending(server._state_root(), now=NOW.timestamp())
assert intent['kind'] == 'daily_checkin'
async def main():
    await server._proactive_tick()
    published = [r for r in server.store.letters if r.get('origin') == 'proactive' and r['letter_status'] == 'COMPLETED']
    assert len(published) == 1, server._proactive_reason
    await server._proactive_tick()
    assert len([r for r in server.store.letters if r.get('origin') == 'proactive' and r['letter_status'] == 'COMPLETED']) == 1
asyncio.run(main())
''')


def test_daily_qq_uses_real_voice_pipeline_without_model_veto(monkeypatch, tmp_path):
    from tests.http.test_chat_jev_proactive import environment, run_contact
    from runtime.personal_chat import backend
    from runtime.personal_chat import service
    from runtime.reply.daily_proactive import daily_candidate
    env = environment(monkeypatch, tmp_path, action='defer', daily=True)
    instant = datetime.fromtimestamp(at(19), BEIJING)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    monkeypatch.setattr(backend, 'datetime', Clock)
    monkeypatch.setattr(service, 'datetime', Clock)
    monkeypatch.setattr(env.server.letters_adapter, '_now', lambda: instant)
    env.snapshot['current']['occurred_at'] = instant.isoformat()
    run_contact(env)
    assert not env.calls  # The daily floor is an explicit application decision.
    assert len(env.sent) == 1 and env.sent[0][0] == 'audio'
    assert env.rows[0]['proactive_decision']['decision']['opportunity_id'].startswith('daily:')
    assert daily_candidate(env.rows, at(19, 1)) is None


def test_daily_channel_uses_recent_responses_and_falls_back(monkeypatch):
    from types import SimpleNamespace
    from runtime.personal_chat.engagement import initiating_channel
    chats = []
    server = SimpleNamespace(store=SimpleNamespace(letters=[], personal_chats=chats),
                             _proactive_settings=lambda: {'enabled': True})
    assert initiating_channel(server, True, at(19)) == 'letter'
    chats.extend(dict(letter_id=str(i), channel='qq', content='synthetic', created_at=at(10)+i)
                 for i in range(3))
    assert initiating_channel(server, True, at(19)) == 'qq'
    assert initiating_channel(server, False, at(19)) == 'letter'


def test_login_worker_ignores_retired_switch():
    from installer.proactive_login import _enabled
    assert _enabled({'enabled': False, 'login_check_enabled': True})
    assert not _enabled({'enabled': True, 'login_check_enabled': False})


def test_daily_opportunity_keeps_limits_and_pending_work():
    from runtime.reply.proactive_letters import make_context
    from runtime.personal_chat.initiative_profile import _BASE
    pending = dict(origin='user', letter_status='PROCESSING', created_at=at(18, 59))
    assert make_context([pending], now=at(19))['blocked']
    yesterday = dict(origin='proactive', letter_status='COMPLETED', created_at=at(20, day=15))
    context = make_context([yesterday], now=at(19), profile=_BASE['reserved'])
    assert context['remaining'] == 0  # Daily floor cannot break the existing rolling cap.
    assert [(p.im_attempt_limit, p.letter_daily_limit) for p in _BASE.values()] == [(3,1),(6,2),(12,2),(24,2),(72,6)]
