"""Development Jev decision through real native context and publication."""
import pytest

from tests.http.test_expression_context_routes import run_isolated
from tests.http.test_proactive_expression_context import SETUP


@pytest.mark.parametrize('case', ['send', 'defer', 'world_changed', 'body_changed', 'pause', 'no_old_letter', 'next_opportunity'])
def test_native_jev_controls_body_and_rechecks_live_authority(tmp_path, case):
    run_isolated(tmp_path, SETUP + '\ncase = ' + repr(case) + r'''
import os
from copy import deepcopy
from runtime.reply.companion_duties import FrozenCompanionDutyResult
from runtime.reply.companion_proactive import JevProactivePort
from runtime.reply.companion_decision import _json
from runtime.reply import companion_duties
os.environ['OLIVIA_JEV_DECISION_URL'] = 'http://127.0.0.1:8097/v1/companion/decide'
class PersonaProbe:
    async def evaluate(self, kind, value):
        assert kind == 'persona'
        return FrozenCompanionDutyResult(decision_json='{"persona_ids":[]}',
            input_digest=hashlib.sha256(_json(value).encode()).hexdigest())
companion_duties.configured_duties = lambda: PersonaProbe()
server.private_world_port = SimpleNamespace(snapshot=lambda: SimpleNamespace(
    relationship_stage='committed', tension=0))
snapshot = {'status':'READY', 'stale':False,
    'rhythm':{'phase':'free', 'availability':'open'},
    'current':{'source_id':'day:reading', 'occurred_at':NOW.isoformat(), 'activity_kind':'reading',
               'activity':'阅读', 'location':'家里', 'note':'在家里阅读。'},
    'world':{'schedule':{'current_class':None}}, 'shared':[]}
server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(snapshot=lambda now: deepcopy(snapshot)))
if case == 'pause':
    server.store.personal_chats.append({'letter_id':'pause', 'channel':'qq', 'origin':'user',
        'content':'别主动打扰我', 'reply_text':'好', 'delivery_status':'DELIVERED',
        'letter_status':'COMPLETED', 'initiative_preference':'pause', 'created_at':NOW.timestamp()-3600})
if case == 'no_old_letter':
    server.store.letters.clear()
server._proactive_ready = lambda: server._proactive_settings()['enabled']
requests = []
observations = []
from runtime.diagnostics import reply_telemetry
reply_telemetry.emit = lambda *args, **kwargs: observations.append(kwargs.get('initiative', {}))
def request(self, encoded):
    value = json.loads(encoded)
    requests.append(value)
    assert value['channel'] == 'letter' and value['available_media'] == ['text']
    assert value['relationship']['tier'] == 'committed'
    item = value['opportunities'][0]
    assert 'contact_guidance' in json.loads(item['description'])
    decision = ({'action':'defer', 'opportunity_id':None, 'reason':'not_now', 'intent':None, 'medium':None}
        if case == 'defer' or case == 'next_opportunity' and len(requests) == 1 else {'action':'send', 'opportunity_id':item['id'], 'reason':'opportunity',
            'intent':item['kind'], 'medium':'text'})
    if case == 'world_changed':
        snapshot['world']['schedule']['current_class'] = {'title':'合成课程'}
    return {'schema_version':'companion-proactive-decision/1', 'decision':decision,
        'input_digest':hashlib.sha256(encoded.encode()).hexdigest(), 'backend':'jev','model':'jev-1.13.0',
        'status':'valid_contract','contract_valid':True,'action_executed':False,'production_approved':False,
        'latency_ms':1,'api_calls':1,'usage':{'input_tokens':1}}
JevProactivePort._request = request
original_complete = complete
titles = []
async def body(messages, **kwargs):
    assert not kwargs['request_id'].endswith(':plan')  # No legacy semantic plan.
    if kwargs['request_id'].endswith(':title'):  # Asked of the finished letter alone.
        titles.append([m['content'] for m in messages])
        return SimpleNamespace(text='{"title":"在家读书的晚上"}')
    wire = '\n'.join(m['content'] for m in messages)
    assert '<proactive_decision>' in wire
    if case == 'body_changed':
        snapshot['rhythm'] = {'phase':'sleep', 'availability':'rest'}
    return await original_complete(messages, **kwargs)
server.letters_adapter.gateway.complete_scoped = body
async def main():
    server._refresh_proactive_context()
    await server._proactive_tick()
    if case == 'next_opportunity':
        server.time.time = lambda: NOW.timestamp() + 1201
        server._refresh_proactive_context()
        await server._proactive_tick()
    published = [r for r in server.store.letters if r.get('origin') == 'proactive' and r['letter_status'] == 'COMPLETED']
    if case in {'send', 'no_old_letter', 'next_opportunity'}:
        assert len(published) == 1, (case, server._proactive_reason, requests)
        assert published[0]['proactive_decision']['decision']['action'] == 'send'
        assert published[0]['title'] == '在家读书的晚上' and titles == [[server.LETTER_TITLE_RULE, '慢慢来就好。']]
        assert len(calls) == 1 and len(requests) == (2 if case == 'next_opportunity' else 1)
    else:
        assert not published, (case, published)
        assert titles == []
        assert len(calls) == (1 if case == 'body_changed' else 0)
        assert len(requests) == (0 if case == 'pause' else 1)
    if case == 'defer':
        assert any(e.get('medium') == 'letter' and e.get('reason') == 'deferred' for e in observations)
    assert not getattr(server, '_jev_contact_channel', None)
asyncio.run(main())
''')
