"""Development letter routing persists Jev decisions before any body or media."""
from tests.http.test_expression_context_routes import run_isolated


def test_jev_preview_photo_reaches_native_attachment_scheduler(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime import image_reply
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import Engine
from letter_triage import TriageResult
row = {'letter_id': 'jev-preview-image', 'content': '看看明天的穿搭',
       'image_reply_settings': {'enabled': True},
       'route_preflight': TriageResult('normal','text_letter','jev_no_explicit_media','completed',True).to_dict()}
old = {'plan': plan(kind='image')}
old['plan']['resolution'].update(status='unsupported', blocked_steps=['s1'])
row.update(error_code='JEV_PLAN_UNSUPPORTED', companion_decision=old)
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
server._commit_private_world_letter = lambda row: False
server.letters_adapter.remember_conversation = lambda *a: None
scheduled = []
image_reply.schedule = lambda server, row: scheduled.append(row['image_status'])
port = Port(plan(kind='image'))
server.reply_pipeline = ReplyPipeline(Engine('给你看看搭配。'), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=port)
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert scheduled == ['PENDING']
assert row['reply_mode'] == 'text_letter'
assert row['input_revision'] == 1 and row['superseded_companion_decision'] == old
assert port.turns[0].input['capabilities']['kinds'] == ['text', 'image']
assert row['companion_delivery'] == 'image'
''')


def test_native_no_reply_persists_without_committing_or_scheduling(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
from types import SimpleNamespace
import local_server as server
from runtime import image_reply
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import Engine

row = {'letter_id': 'jev-native', 'content': '这条不用回复', 'image_reply_settings': {'enabled': True}}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
calls = []
server._commit_private_world_letter = lambda row: calls.append('world')
server.letters_adapter.remember_conversation = lambda *a: calls.append('memory')
image_reply.schedule = lambda *a: calls.append('photo')
engine, port = Engine('must not generate'), Port(plan(timing='no_reply'))
server.reply_pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=port)
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert row['letter_status'] == 'SKIPPED' and row.get('reply_text', '') == ''
assert row['companion_timing'] == 'no_reply' and not calls and not engine.requests
assert row['companion_decision']['source_id_map'] == {'t1': 'reply:jev-native:user'}
server.store.letters.clear()
server._load_store_state()
assert server.store.letters[0]['companion_decision']['plan']['proposal']['timing'] == 'no_reply'
''')


def test_native_writer_failure_reuses_saved_jev_on_retry(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from reply_orchestrator import ReplyResult, ReplyState
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port

row = {'letter_id': 'jev-retry', 'content': '我醒了'}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
class FailedWriter:
    async def run(self, request):
        assert row.get('companion_decision')
        return ReplyResult(request.request_id, ReplyState.FAILED, error_code='PROVIDER_TIMEOUT')
port = Port()
server.reply_pipeline = ReplyPipeline(FailedWriter(), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=port)
for _ in range(2):
    assert not asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert len(port.turns) == 1
assert row['letter_status'] == 'FAILED' and row['error_code'] == 'LLM_TIMEOUT'
''')


def test_unsupported_valid_plan_is_saved_and_answered_in_text(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import Engine

row = {'letter_id': 'jev-two-assets', 'content': '分两条发来'}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
value = plan()
value['proposal']['steps'].append(dict(id='s2', medium='text', parts=[dict(kind='text', content_ref='c1')],
    after=[dict(step_id='s1', event='delivered')], requirement_ids=[]))
engine, port = Engine('这次先用文字回你。'), Port(value)
server.reply_pipeline = ReplyPipeline(engine, reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=port)
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert len(port.turns) == 1 and len(engine.requests) == 1
assert '只能用文字回复' in str(engine.requests[0].messages)
assert not row.get('error_code')
assert len(row['companion_decision']['plan']['proposal']['steps']) == 2
''')


def test_jev_text_is_published_and_the_photo_planner_still_decides(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime import image_reply
from original_client_letter_contract import serialize_letter_detail
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port
from tests.persona.test_reply_semantic_wiring import Engine

row = {'letter_id': 'jev-text', 'content': '我醒了', 'image_reply_settings': {'enabled': True}}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
server._commit_private_world_letter = lambda row: False
server.letters_adapter.remember_conversation = lambda *a: None
scheduled = []
image_reply.schedule = lambda *a: scheduled.append('photo')
server.reply_pipeline = ReplyPipeline(Engine('醒啦。'), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=Port())
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
# A plain JEV text turn still lets the photo planner decide, as before 2.0. The
# written reply is kept while that decision runs, then shown with or without a photo.
assert row['letter_status'] == 'COMPLETED' and row['reply_text'] == '醒啦。'
assert 'image_status' not in row and scheduled == ['photo']
row['image_status'] = 'SKIPPED'
detail = serialize_letter_detail(row)
assert detail['letterStatus'] == 4 and detail.get('replyBody', detail['replyText']) == '醒啦。'
''')


def test_secondary_photo_only_on_plain_turns():
    from runtime.image_reply import secondary_photo_allowed
    def plan(requirements, extras=True):
        return {'plan': {'understanding': {'requirements': requirements, 'extras_allowed': extras},
                         'resolution': {'uncertain_fields': []}}}
    plain = {'companion_decision': plan([]), 'companion_delivery': 'text'}
    asked = {'companion_decision': plan([{'id': 'r1', 'fulfillment': 'current'}]), 'companion_delivery': 'audio_speech'}
    restricted = {'companion_decision': plan([], extras=False), 'companion_delivery': 'text'}
    assert not secondary_photo_allowed(restricted)  # the user limited extra media
    assert secondary_photo_allowed({}) and secondary_photo_allowed(plain)
    assert not secondary_photo_allowed(asked)


def test_auxiliary_text_recovery_marks_photo_skipped_and_publishes_reviewed_body(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime import image_reply
from original_client_letter_contract import serialize_letter_detail
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import ReviewVerdict
from tests.persona.test_jev_pipeline import Port
from tests.persona.test_reply_semantic_wiring import Engine, Reviewer
row = {'letter_id': 'jev-recovery-text', 'content': '早安', 'image_reply_settings': {'enabled': True}}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
server._commit_private_world_letter = lambda row: False
server.letters_adapter.remember_conversation = lambda *a: None
reviewer = Reviewer(ReviewVerdict.PASS)
server.reply_pipeline = ReplyPipeline(Engine('早上好，我收到啦。'), reviewer=reviewer,
    rewriter=UnavailableRewriter(), discover_runtime_ports=False,
    companion_decision_port=Port(error='JEV_UNAVAILABLE'))
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert row['letter_status'] == 'COMPLETED' and row['image_status'] == 'SKIPPED'
assert row['degraded_stages'] == {'decision': 'JEV_UNAVAILABLE'}
assert len(reviewer.seen) == 1 and not image_reply._jobs
detail = serialize_letter_detail(row)
assert detail['letterStatus'] == 4 and '早上好' in detail.get('replyBody', detail['replyText'])
''')


def test_native_letter_review_retry_after_pipeline_restart_resumes_private_body(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import ReviewVerdict
from tests.persona.test_jev_pipeline import Port
from tests.persona.test_reply_semantic_wiring import Engine, Reviewer
row = {'letter_id': 'jev-private-writer', 'content': '早安', 'image_reply_settings': {'enabled': False}}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
server._commit_private_world_letter = lambda row: False
server.letters_adapter.remember_conversation = lambda *a: None
engine = Engine('只生成一次的候选。')
server.reply_pipeline = ReplyPipeline(engine, reviewer=Reviewer(RuntimeError('synthetic outage')),
    rewriter=UnavailableRewriter(), discover_runtime_ports=False,
    companion_decision_port=Port(), recovery_root=server._local_data_root())
assert not asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert row['letter_status'] == 'FAILED' and not row.get('reply_text')
frozen_time = row['generation_context_at']
fresh = Engine('不能再次生成。')
reviewer = Reviewer(ReviewVerdict.PASS)
server.reply_pipeline = ReplyPipeline(fresh, reviewer=reviewer, rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=Port(), recovery_root=server._local_data_root())
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
assert row['reply_text'] == '只生成一次的候选。'
assert len(engine.requests) == 1 and not fresh.requests and len(reviewer.seen) == 1
assert row['generation_context_at'] == frozen_time and row['stage_cache_hits']['writer'] == 1
''')


def test_plain_jev_letter_reaches_the_photo_planner_and_never_waits_forever(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from runtime import image_reply
    from original_client_letter_contract import serialize_letter_detail
    def plan(requirements):
        return {'plan': {'understanding': {'requirements': requirements, 'extras_allowed': True},
                         'resolution': {'uncertain_fields': []}}}
    server = SimpleNamespace(_persist_store_state=lambda: None)
    reached = []
    async def planner(server, row, *a, **k):
        reached.append(row['letter_id'])
        row['image_status'] = 'SKIPPED'
    monkeypatch.setattr(image_reply, '_prepare_once', planner)
    base = dict(letter_status='COMPLETED', reply_mode='text_letter', reply_text='醒啦。',
                image_reply_settings={'enabled': True})
    plain = dict(base, letter_id='plain', companion_decision=plan([]), companion_delivery='text')
    asked = dict(base, letter_id='asked', companion_decision=plan([{'id': 'r1', 'fulfillment': 'current'}]),
                 companion_delivery='audio_speech')
    for row in (plain, asked):
        asyncio.run(image_reply.prepare(server, row, '我醒了', row['reply_text']))
    assert reached == ['plain']
    # A turn whose medium JEV owns gets a final photo state, so the written letter is shown.
    assert asked['image_status'] == 'SKIPPED'
    assert serialize_letter_detail(asked)['letterStatus'] == 4
    assert serialize_letter_detail(plain)['letterStatus'] == 4


def test_requested_voice_turns_a_text_letter_into_a_voice_reply(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import Engine

row = {'letter_id': 'voice-asked', 'content': '可是拍到的三个人真的很菜啊\n语音回复',
       'reply_routes': {'voice_reply': True, 'singing_video': False, 'voice_song_video': False},
       'image_reply_settings': {'enabled': False}}
server.store.letters[:] = [row]
server.store.personal_chats[:] = []
server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
server._commit_private_world_letter = lambda row: False
server.letters_adapter.remember_conversation = lambda *a: None
scheduled = []
server._schedule_media_job = lambda letter_id, content, text, mode: scheduled.append(mode)
port = Port(plan(kind='audio_speech'))
server.reply_pipeline = ReplyPipeline(Engine('是有点菜，不过笑死我了。'), reviewer=NullReviewer(), rewriter=UnavailableRewriter(),
    discover_runtime_ports=False, companion_decision_port=port)
assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
# The route classifier said "text letter"; JEV heard "语音回复". The user gets a voice reply.
assert row['letter_status'] == 'COMPLETED' and row['reply_mode'] == 'voice_reply'
assert row['reply_video_enabled'] is False and row['media_status'] == 'PENDING'
assert scheduled == ['voice_reply']
''')


def test_letters_are_spoken_by_default_when_voice_replies_are_on(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
import local_server as server
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer
from tests.persona.test_jev_pipeline import Port, plan
from tests.persona.test_reply_semantic_wiring import Engine

server.daily_life_runtime = None
server._current_life_rhythm = lambda: {}
server._schedule_text_reply_delay = lambda *a: None
server._commit_private_world_letter = lambda row: False
server.letters_adapter.remember_conversation = lambda *a: None
scheduled = []
server._schedule_media_job = lambda letter_id, content, text, mode: scheduled.append((letter_id, mode))
limited = plan()
# The user asked for a text reply this time (e.g. "这次回文字就好").
limited['understanding'].update(extras_allowed=False, requirements=[dict(id='r1', fulfillment='current',
    alternatives=[dict(kinds=['text'], min_assets=1, max_assets=1)], evidence_turn_ids=['t1'])])
limited['proposal']['steps'][0]['requirement_ids'] = ['r1']
for letter_id, voice_on, decision, expected in (('plain-voice-on', True, plan(), 'voice_reply'),
                                                ('plain-voice-off', False, plan(), 'text_letter'),
                                                ('text-only-asked', True, limited, 'text_letter')):
    row = {'letter_id': letter_id, 'content': '今天考完试了，好累',
           'reply_routes': {'voice_reply': voice_on, 'singing_video': False, 'voice_song_video': False},
           'image_reply_settings': {'enabled': False}}
    server.store.letters[:] = [row]
    server.store.personal_chats[:] = []
    server.reply_pipeline = ReplyPipeline(Engine('辛苦啦，先好好睡一觉。'), reviewer=NullReviewer(),
        rewriter=UnavailableRewriter(), discover_runtime_ports=False, companion_decision_port=Port(decision))
    assert asyncio.run(server.generate_reply(row['letter_id'], row['content']))
    assert row['letter_status'] == 'COMPLETED' and row['reply_mode'] == expected, (letter_id, row['reply_mode'])
    assert row['companion_delivery'] == 'text'
    if expected == 'voice_reply':
        assert row['reply_video_enabled'] is False and row['media_status'] == 'PENDING'
assert scheduled == [('plain-voice-on', 'voice_reply')]
''')
