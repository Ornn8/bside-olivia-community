"""Frozen proactive views exercised through actual assembly and gateway calls."""
import pytest

from tests.http.test_expression_context_routes import run_isolated


SETUP = r'''
import asyncio, json, hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
import local_server as server
from persona_assembly import UntrustedFragment
from runtime.reply.reply_context import ReplyMode
from runtime.reply.proactive_letters import write_json
from runtime.reply.character_emotion_context import checked_expression_context
from tests.persona.test_reply_pipeline import ROOT, _configured_v2_pipeline
NOW = datetime(2026, 9, 27, 2, tzinfo=timezone.utc)
_, _, bridge, _ = _configured_v2_pipeline(ROOT / 'linli_character/persona_release_v2.json')
server.letters_adapter = bridge.adapter
source = {'letter_id':'source', 'content':'过去的问候', 'reply_text':'过去的回信',
          'reply_revision':1, 'letter_status':'COMPLETED', 'created_at':NOW.timestamp()-7200,
          'expression_context': {'private_old_view':'OLD_SOURCE_MUST_NOT_REAPPEAR'}}
server.store.letters[:] = [source]
server.store.personal_chats[:] = []
server._proactive_ready = lambda: True
server._commit_private_world_letter = lambda row: False
server._schedule_daily_life_exchange = lambda row: None
write_json(server._state_root() / 'proactive/settings.json', {'enabled':True})
state, reads, calls = {'location':'琴房'}, [], []
def life(content, *, recent_fragments=None, now=None):
    reads.append(('world', content, now))
    return (UntrustedFragment('linli.daily-life', json.dumps({
        'kind':'character_life_reference', 'stale':False,
        'current':{'location':state['location'], 'activity':'慢练',
                   'source_id':'day:synthetic', 'evidence_kind':'published_life'}}, ensure_ascii=False)),)
async def emotion(content, *, now):
    assert content is None  # An opportunity is not a newly received letter.
    reads.append(('emotion', content, now))
    return {'reaction_subject':'character', 'interpretation_only':True,
            'reactions':[{'reaction':'relieved', 'quote':'SYNTHETIC_CURRENT_VIEW'}]}
server.letters_adapter.daily_life_fragments = life
server.letters_adapter.prepare_character_emotion = emotion
async def complete(messages, *, request_id, scope):
    calls.append((tuple(dict(m) for m in messages), request_id, scope))
    if request_id.endswith(':plan'):
        return SimpleNamespace(text='{"decision":"send","format":"text","title":"问候"}')
    # Publication has its own actual delivery timestamp. The assertion clock
    # remains armed throughout body assembly and until its provider request.
    server.letters_adapter._now = lambda: NOW
    return SimpleNamespace(text='慢慢来就好。\n[[signature:林离]]')
server.letters_adapter.gateway = SimpleNamespace(complete_scoped=complete,
    timeout_seconds_for_scope=lambda *a, **kw: 10)
from datetime import timedelta
from runtime.reply.proactive_letters import scan_pending
server.time.time = lambda: NOW.timestamp()
opportunity_world = {'shared':[{'id':'synthetic-shared', 'actor':'user', 'status':'planned',
    'source_id':'reply:source:1', 'updated_at':(NOW-timedelta(days=2)).isoformat()}]}
server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(exchange_state=lambda: opportunity_world))
server._refresh_proactive_context()
intent = scan_pending(server._state_root(), now=NOW.timestamp())
assert intent.get('kind') == 'shared_followup'
'''


@pytest.mark.parametrize('legacy', [False, True])
def test_plan_and_body_share_actual_frozen_views_without_rereading_world_clock_or_source_view(tmp_path, legacy):
    run_isolated(tmp_path, SETUP + '\nlegacy = ' + repr(legacy) + r'''
if legacy:
    from dataclasses import replace
    server.letters_adapter.config = replace(server.letters_adapter.config, persona_v2_enabled=False)
async def main():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    assert calls == [] and reads == [('world','',NOW),('emotion',None,NOW)]
    plan = json.loads(await server._proactive_complete(intent, planning=True, turn=turn))
    server.load_persona = lambda *a: (_ for _ in ()).throw(AssertionError('persona reloaded after planning'))
    state['location'] = '随后去了校园'
    server.letters_adapter._now = lambda: (_ for _ in ()).throw(AssertionError('live clock read'))
    server.letters_adapter.daily_life_fragments = lambda *a, **k: (_ for _ in ()).throw(AssertionError('live world read'))
    server.letters_adapter.prepare_character_emotion = lambda *a, **k: (_ for _ in ()).throw(AssertionError('second emotion read'))
    await server._publish_proactive(intent, plan, turn=turn)
    assert len(calls) == 2
    common = turn['common_blocks']
    for messages, _, _ in calls:
        contents = [m['content'] for m in messages]
        assert tuple(content for content in contents if content in common) == common
        assert all(contents.count(content) == 1 for content in common)
        wire = '\n'.join(contents)
        assert '随后去了校园' not in wire and 'OLD_SOURCE_MUST_NOT_REAPPEAR' not in wire
        assert json.loads(messages[-1]['content'])['now'] == NOW.isoformat()
    row = server.store.letters[-1]
    assert row['origin'] == 'proactive' and row['reply_text'] == '慢慢来就好。'
    snapshot = checked_expression_context(row)
    assert snapshot and snapshot['as_of'] == NOW.isoformat()
    assert snapshot['world']['current']['location'] == '琴房'
    assert snapshot['emotion_used'] is True and snapshot['binding']['reply_revision'] == 1
    assert snapshot['binding']['reply_sha256'] == hashlib.sha256(row['reply_text'].encode()).hexdigest()
asyncio.run(main())
''')


@pytest.mark.parametrize('phase', ['body', 'draft'])
@pytest.mark.parametrize('change', ['completed', 'expired', 'unread', 'quota', 'other_pending', 'unchanged'])
def test_real_opportunity_and_publication_rules_are_rechecked_after_each_wait(tmp_path, phase, change):
    run_isolated(tmp_path, SETUP + '\nphase, change = ' + repr((phase, change)) + r'''
from datetime import timedelta
from runtime.reply.proactive_letters import scan_pending
clock = [NOW.timestamp()]
server.time.time = lambda: clock[0]
shared = {'shared':[{'id':'synthetic-shared', 'actor':'user', 'status':'planned',
    'source_id':'reply:source:1', 'updated_at':(NOW-timedelta(days=2)).isoformat()}]}
server.daily_life_runtime = SimpleNamespace(store=SimpleNamespace(exchange_state=lambda: shared))
write_json(server._state_root() / 'proactive/settings.json', {'enabled':True, 'allow_voice':True})
server._refresh_proactive_context()
intent = scan_pending(server._state_root(), now=clock[0])
assert intent.get('kind') == 'shared_followup'
def change_opportunity():
    if change == 'completed':
        shared['shared'][0]['status'] = 'completed'
    elif change == 'expired':
        clock[0] += 6 * 86400
    elif change in ('unread','quota'):
        server.store.letters.append({'letter_id':'other-proactive', 'origin':'proactive',
            'letter_status':'COMPLETED', 'is_read':int(change == 'quota'),
            'created_at':clock[0] - (36 * 3600 if change == 'unread' else 0),
            'proactive_candidate_id':'another-opportunity'})
        if change == 'unread':
            server.store.letters.append({**server.store.letters[-1], 'letter_id':'second-unread'})
    elif change == 'other_pending':
        # Even a different row carrying the draft id is not the draft object.
        other_id = (next(row['letter_id'] for row in server.store.letters
                    if row.get('proactive_candidate_id') == intent['id'])
                    if phase == 'draft' else 'other-pending')
        server.store.letters.append({'letter_id':other_id, 'content':'另一个待答输入',
            'letter_status':'PENDING', 'created_at':clock[0]})
original_complete = complete
async def during_body(messages, **kwargs):
    if phase == 'body' and kwargs['request_id'].endswith(':body'):
        change_opportunity()
    return await original_complete(messages, **kwargs)
server.letters_adapter.gateway.complete_scoped = during_body
saved_draft = []
original_persist = server._persist_store_state
def persist_with_draft():
    original_persist()
    draft = next((row for row in server.store.letters if row.get('proactive_candidate_id') == intent['id']), None)
    if phase == 'draft' and draft is not None and not saved_draft:
        # The last moment before publication: the hidden draft has just been saved.
        saved_draft.append(draft)
        assert draft['letter_status'] == 'PROCESSING' and 'expression_context' not in draft
        change_opportunity()
server._persist_store_state = persist_with_draft
async def main():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    original_common = turn['common_blocks']
    await server._proactive_complete(intent, planning=True, turn=turn)
    await server._publish_proactive(intent, {'format':'text', 'title':'问候'}, turn=turn)
    own = [row for row in server.store.letters if row.get('proactive_candidate_id') == intent['id']]
    assert bool(saved_draft) is (phase == 'draft')
    if change == 'unchanged':
        assert len(own) == 1 and own[0]['letter_status'] == 'COMPLETED'
        assert checked_expression_context(own[0])
    else:
        assert not any(row['letter_status'] == 'COMPLETED' or 'expression_context' in row for row in own)
        if phase == 'draft':
            assert len(own) == 1 and own[0]['letter_status'] == 'CANCELED'
        else:
            assert own == []
    assert len(calls) == 2 and turn['common_blocks'] == original_common
    assert reads == [('world','',NOW),('emotion',None,NOW)]
    assert not server._proactive_busy
asyncio.run(main())
''')


def test_proactive_selects_persona_before_planning_and_reuses_it_for_body(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
from pathlib import Path
asset = server._state_root() / 'frozen-persona.json'
payload = json.loads(Path(server.letters_adapter.persona_v2_path).read_text(encoding='utf-8'))
asset.write_text(json.dumps(payload), encoding='utf-8')
server.letters_adapter.persona_v2_path = asset
original = next(item['statement'] for item in payload['declarations'] if item['declaration_id'] == 'anchor.reading')
selection_calls = []
async def select(messages, **kwargs):
    selection_calls.append((messages, kwargs))
    assert kwargs['request_id'].startswith('history-select:')
    return SimpleNamespace(text=json.dumps({'selected_ids':[], 'dependencies':[], 'persona_ids':['anchor.reading']}))
server.letters_adapter.gateway.complete_structured_scoped = select
async def main():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    assert len(selection_calls) == 1 and calls == []
    plan = json.loads(await server._proactive_complete(intent, planning=True, turn=turn))
    next(item for item in payload['declarations'] if item['declaration_id'] == 'anchor.reading')['statement'] = '后来替换的文字。'
    asset.write_text(json.dumps(payload), encoding='utf-8')
    server.letters_adapter.reply_context_messages = lambda *a, **k: (_ for _ in ()).throw(AssertionError('body reassembled'))
    await server._publish_proactive(intent, plan, turn=turn)
    assert len(selection_calls) == 1 and len(calls) == 2
    for messages, _, _ in calls:
        wire = '\n'.join(message['content'] for message in messages)
        assert original in wire and '后来替换的文字。' not in wire
        assert 'anchor.current_piece' not in wire
        assert 'constitution.autonomy' in wire
    assert checked_expression_context(server.store.letters[-1])
asyncio.run(main())
''')


@pytest.mark.parametrize('case', ['oversize_world', 'oversize_emotion', 'both'])
def test_common_projection_budget_omission_is_identical_in_both_provider_requests(tmp_path, case):
    run_isolated(tmp_path, SETUP + '\ncase = ' + repr(case) + r'''
from dataclasses import replace
server.letters_adapter.config = replace(server.letters_adapter.config, max_input_chars=18000)
if case in ('oversize_world','both'):
    original_life = server.letters_adapter.daily_life_fragments
    def huge_life(*a, **k):
        values = original_life(*a, **k)
        world = json.loads(values[0].text)
        world['last_observation'] = {'note':'合成资料'*8000}
        return (UntrustedFragment('linli.daily-life', json.dumps(world, ensure_ascii=False)),)
    server.letters_adapter.daily_life_fragments = huge_life
if case in ('oversize_emotion','both'):
    original_emotion = server.letters_adapter.prepare_character_emotion
    async def huge_emotion(*a, **k):
        view = await original_emotion(*a, **k)
        view['reactions'][0]['quote'] = '合成旧反应'*2000
        return view
    server.letters_adapter.prepare_character_emotion = huge_emotion
async def main():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    plan = json.loads(await server._proactive_complete(intent, planning=True, turn=turn))
    await server._publish_proactive(intent, plan, turn=turn)
    snapshot = checked_expression_context(server.store.letters[-1])
    assert snapshot and snapshot['world_used'] == (case == 'oversize_emotion')
    assert snapshot['emotion_used'] == (case == 'oversize_world')
    for messages, _, _ in calls:
        wire = '\n'.join(m['content'] for m in messages)
        assert ('<character_emotion>' in wire) == snapshot['emotion_used']
        assert ('linli.daily-life' in wire) == snapshot['world_used']
        assert sum(len(m['content']) for m in messages) <= 18000
asyncio.run(main())
''')


@pytest.mark.parametrize('failure', ['body_overflow', 'cancel', 'disabled', 'source_changed', 'source_changed_during_body'])
def test_invalidated_or_failed_turn_never_publishes_context_or_calls_another_plan(tmp_path, failure):
    run_isolated(tmp_path, SETUP + '\nfailure = ' + repr(failure) + r'''
async def main():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    plan = json.loads(await server._proactive_complete(intent, planning=True, turn=turn))
    if failure == 'body_overflow':
        # The body consumes the frozen context, never a second live assembly.
        # A corrupted/oversize frozen value must fail before another paid call.
        turn['context_messages'] = ({'role':'system','content':'X'*100001},)
    elif failure == 'source_changed':
        source['reply_revision'] = 2
    else:
        async def interrupted(*a, **k):
            calls.append(((), k['request_id'], k['scope']))
            if failure == 'cancel':
                raise asyncio.CancelledError()
            if failure == 'source_changed_during_body':
                source['reply_revision'] = 2
            else:
                write_json(server._state_root() / 'proactive/settings.json', {'enabled':False})
            return SimpleNamespace(text='不会发布的正文。')
        server.letters_adapter.gateway.complete_scoped = interrupted
    try:
        await server._publish_proactive(intent, plan, turn=turn)
    except (ValueError, asyncio.CancelledError):
        pass
    assert len([c for c in calls if c[1].endswith(':plan')]) == 1
    assert len(calls) == (2 if failure in ('cancel','disabled','source_changed_during_body') else 1)
    assert server.store.letters == [source] and not server._proactive_busy
asyncio.run(main())
''')


def test_cancellation_after_body_before_publication_does_not_persist_an_adopted_view(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
write_json(server._state_root() / 'proactive/settings.json', {'enabled':True, 'allow_voice':True})
original_persist = server._persist_store_state
interrupted = []
def persist_then_cancel():
    original_persist()
    draft = server.store.letters[-1]
    if draft.get('origin') == 'proactive' and draft['letter_status'] == 'PROCESSING' and not interrupted:
        interrupted.append(draft)
        assert 'expression_context' not in draft
        raise asyncio.CancelledError()
server._persist_store_state = persist_then_cancel
async def main():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    try:
        await server._publish_proactive(intent, {'format':'text','title':'问候'}, turn=turn)
    except asyncio.CancelledError:
        pass
    row = server.store.letters[-1]
    assert interrupted and row['origin'] == 'proactive' and row['letter_status'] == 'FAILED'
    assert 'expression_context' not in row
    saved = json.loads((server._state_root() / 'state.json').read_text(encoding='utf-8'))['letters'][-1]
    assert saved['letter_status'] == 'FAILED' and 'expression_context' not in saved
    assert len(calls) == 1 and not server._proactive_busy
asyncio.run(main())
''')


@pytest.mark.parametrize('phase,change', [
    ('planning','new_im'), ('body','new_im'), ('draft','new_im'),
    ('draft','earlier_revision'), ('draft','background'),
])
def test_im_input_changes_cancel_frozen_turn_but_background_updates_do_not(tmp_path, phase, change):
    run_isolated(tmp_path, SETUP + '\nphase, change = ' + repr((phase, change)) + r'''
from runtime.personal_chat.service import PersonalChatService
from runtime.personal_chat.events import PersonalMessage
write_json(server._state_root() / 'proactive/settings.json', {'enabled':True, 'allow_voice':True})
async def unused(*a, **k):
    raise AssertionError('This fixture only accepts incoming IM, never generates or sends it.')
service = PersonalChatService(server.store.personal_chats, server._persist_store_state,
    unused, unused, {'qq':('bot','owner')})
async def change_input():
    if change == 'new_im':
        await service.ingest(PersonalMessage('qq','bot','owner','new','我刚醒了'))
    elif change == 'earlier_revision':
        # The older pending row may be revised by merge while a newer row exists.
        first = server.store.personal_chats[0]
        first['input_revision'] = first.get('input_revision', 0) + 1
    else:
        first = server.store.personal_chats[0]
        first.update(delivery_status='DELIVERED', reply_text='后台完成的回复',
                     reply_revision=1, media_status='COMPLETED', expression_context={'background':True})
        server.store.personal_chats.reverse()  # Storage order is not new input.
original_complete = complete
async def during_gateway(messages, **kwargs):
    if (phase == 'planning' and kwargs['request_id'].endswith(':plan')
            or phase == 'body' and kwargs['request_id'].endswith(':body')):
        await change_input()
    return await original_complete(messages, **kwargs)
server.letters_adapter.gateway.complete_scoped = during_gateway
original_persist = server._persist_store_state
changed = []
def persist_with_draft():
    original_persist()
    draft = next((row for row in server.store.letters if row.get('origin') == 'proactive'), None)
    if phase == 'draft' and draft is not None and not changed:
        # The last moment before publication, with no await left to ingest through.
        changed.append(draft)
        if change == 'new_im':
            server.store.personal_chats.append({'letter_id':'new', 'content':'我刚醒了', 'channel':'qq',
                'source_messages':{'new':'我刚醒了'}, 'delivery_status':'RECEIVED', 'letter_status':'PROCESSING',
                'created_at':NOW.timestamp()})
        else:
            first = server.store.personal_chats[0]
            if change == 'earlier_revision':
                first['input_revision'] = first.get('input_revision', 0) + 1
            else:
                first.update(delivery_status='DELIVERED', reply_text='后台完成的回复',
                             reply_revision=1, media_status='COMPLETED', expression_context={'background':True})
                server.store.personal_chats.reverse()  # Storage order is not new input.
server._persist_store_state = persist_with_draft
async def main():
    await service.ingest(PersonalMessage('qq','bot','owner','old-first','先前的一句'))
    await service.ingest(PersonalMessage('qq','bot','owner','old-last','先前的第二句'))
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    await server._proactive_complete(intent, planning=True, turn=turn)
    await server._publish_proactive(intent, {'format':'text','title':'问候'}, turn=turn)
    published = [row for row in server.store.letters if row.get('origin') == 'proactive']
    assert bool(changed) is (phase == 'draft')
    if change == 'background':
        assert len(published) == 1 and published[0]['letter_status'] == 'COMPLETED'
        assert checked_expression_context(published[0])
    else:
        assert not any(row['letter_status'] == 'COMPLETED' or 'expression_context' in row for row in published)
        assert len(calls) == (1 if phase == 'planning' else 2)
    assert all('im_input_signature' not in str(messages) for messages, _, _ in calls)
    assert not server._proactive_busy
asyncio.run(main())
''')
