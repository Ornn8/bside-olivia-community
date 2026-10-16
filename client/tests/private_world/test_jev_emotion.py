import asyncio
from datetime import timedelta
import pytest
from runtime.private_world.character_emotion_runtime import CharacterEmotionRuntime
from runtime.private_world.daily_life import DailyLifeStore
from tests.private_world.test_character_emotion_runtime import NOW, receipt


class Decisions:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        if purpose == 'character-current-affect':
            return {'label': 'calm', 'reason': 'body', 'intensity': 'low'}
        if self.fail:
            raise RuntimeError('JEV_UNAVAILABLE')
        template = dict(reaction='frustrated', quote='q0', need='rest', action='rest', reported='none',
                    reported_quote='q0', concern='none', concern_action='open', revision='none')
        return {key: 'none' if key.startswith('lifecycle_') or '_existing_' in key else ('calm' if key == 'affect_label' else 'low' if key == 'affect_intensity' else 'body') if key.startswith('affect_')
                else template[key.split('_', 1)[1]] for key in questions}


@pytest.mark.parametrize('kind', ['received_input', 'published_world'])
@pytest.mark.parametrize('wording', ['我明天的面试还没准备好，担心到时说不清楚。', '明天怎么办？', '准备事项' * 30])
def test_new_concern_is_a_short_topic_and_keeps_exact_evidence(kind, wording):
    from runtime.private_world.jev_emotion import appraise
    class Opening(Decisions):
        async def ask(self, *args, **kwargs):
            return {**await super().ask(*args, **kwargs), 'lifecycle_0': 'open'}
    source = dict(source_id='new', source_kind=kind, text=wording, occurred_at=NOW.isoformat())
    packet = dict(persona='', assessment=dict(sources=[source], source_contexts={
        'new': dict(concerns=[], prior_appraisals=[])}))
    port = Opening()
    item = asyncio.run(appraise(port, packet))['appraisals'][0]
    summary = item['concern']['summary']
    assert summary != item['quote']
    assert len(summary) <= 30
    assert summary.endswith('」相关事项')
    assert summary.startswith('用户说：' if kind == 'received_input' else '关于')
    assert item['quote'] in wording
    assert len(port.calls) == 1


def test_resolved_concern_status_remains_visible_to_independent_action_question():
    from runtime.private_world.jev_emotion import prepare
    source = dict(source_id='new', text='这件事已经解决了。', occurred_at=NOW.isoformat())
    concern = dict(id='emotion:old', summary='之前的担心。', status='resolve', occurred_at=NOW.isoformat())
    plan = prepare(dict(persona='', assessment=dict(sources=[source], source_contexts={
        'new': dict(concerns=[concern], prior_appraisals=[])})))
    state = plan[0]
    reference = state['sources']['s0']['concerns']['c0']['shared_context']
    assert state['shared_context'][reference]['status'] == 'resolve'
    assert 'resolve' not in plan[1]['s0_existing_c0']['criteria']


def test_evidence_catalog_preserves_full_sources_and_reduces_repeated_criteria():
    import copy, json
    from runtime.private_world.jev_emotion import appraise
    text = '本次练习进展不顺，需要继续休息。' * 6
    concern = {'id': 'c-old', 'summary': '担心练习进度。' * 25, 'occurred_at': '2026-09-28T04:00:00+00:00'}
    prior = {'source_id': 'old', 'quote': '上次遇到困难。' * 25, 'reaction': 'concerned', 'occurred_at': '2026-09-28T03:00:00+00:00'}
    source = {'source_id': 'new', 'text': text, 'occurred_at': '2026-09-28T05:00:00+00:00'}
    packet = {'persona': '学生', 'assessment': {'sources': [source], 'source_contexts': {'new': {
        'prior_appraisals': [prior], 'concerns': [concern], 'rhythm': {'phase': 'day'}}}}}
    port = Decisions()
    result = asyncio.run(appraise(port, packet))
    state, questions, purpose = port.calls[0]
    shared = state['shared_context']
    state = state['sources']['s0']
    assert state['source'] == source
    assert shared[state['prior_appraisals']['p0']['shared_context']] == prior
    assert shared[state['concerns']['c0']['shared_context']] == concern
    assert 'prior_appraisals' not in state['context'] and 'concerns' not in state['context']
    assert 'preceding_batch_appraisals' not in state
    for item in state['quote_catalog'].values():
        assert source['text'][item['start']:item['end']]
    before = copy.deepcopy(questions)
    for question in before.values():
        for key, value in list(question['criteria'].items()):
            if isinstance(value, dict) and 'quote_id' in value:
                span = state['quote_catalog'][value['quote_id']]
                question['criteria'][key] = text[span['start']:span['end']]
            elif isinstance(value, dict) and 'concern_id' in value:
                question['criteria'][key] = concern
            elif isinstance(value, dict) and 'prior_id' in value:
                question['criteria'][key] = prior
    def size(q):
        return len(json.dumps({'state': state, 'questions': q, 'purpose': purpose}, ensure_ascii=False, separators=(',', ':')).encode())
    assert size(questions) < size(before)
    print('emotion_repeated_criteria_bytes', size(before), 'reference_bytes', size(questions))
    assert result['appraisals'][0]['quote'] == text[:state['quote_catalog']['q0']['end']]


@pytest.mark.parametrize('fail', [False, True])
def test_configured_jev_owns_emotion_and_never_calls_text_model(tmp_path, monkeypatch, fail):
    from runtime.reply import jev_questions
    decisions = Decisions(fail)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda:decisions)
    class Writer:
        async def complete_structured_scoped(self, *args, **kwargs):
            pytest.fail('Text model must not judge emotion in Jev development mode')
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'life.sqlite3'), Writer, lambda:'大学生')
    view = asyncio.run(runtime.evaluate_received([receipt()], now=NOW))
    assert len(decisions.calls) == 1
    if fail:
        assert view['pending_current_input'] and not view['reactions']
    else:
        assert view['reactions'][0]['reaction'] == 'frustrated'
        assert view['reactions'][0]['goal_or_need'] == '休息与恢复精力'
        reopened = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'life.sqlite3'), Writer, lambda:'大学生')
        assert reopened.view(NOW)['reactions'] == view['reactions']


def test_need_without_evidence_fails_closed_without_second_paid_request(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    class Conflicting(Decisions):
        async def ask(self, state, questions, *, purpose):
            answers = await super().ask(state, questions, purpose=purpose)
            return {**answers, 's0_reaction':'none', 's0_quote':'none'}
    port = Conflicting()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda:port)
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'life.sqlite3'), lambda:object(), lambda:'大学生')
    asyncio.run(runtime.evaluate_received([receipt()], now=NOW))
    assert runtime.error_code == 'EMOTION_EVALUATION_UNAVAILABLE'
    assert runtime.store.pending_source_ids(before=NOW)
    with runtime.life_store._db() as db:
        assert db.execute('SELECT COUNT(*) FROM character_emotion_appraisals').fetchone()[0] == 0
    assert len(port.calls) == 1


def test_automatic_failure_backoff_survives_restart_but_new_message_is_not_gated(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    port = Decisions(fail=True)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda:port)
    store = DailyLifeStore(tmp_path/'life.sqlite3')
    runtime = CharacterEmotionRuntime(store, lambda:object(), lambda:'大学生')
    original = receipt()
    runtime.store.receive(original.source_id, original.user_message, occurred_at=NOW)
    asyncio.run(runtime.refresh_world(NOW))
    assert len(port.calls) == 1
    restarted = CharacterEmotionRuntime(store, lambda:object(), lambda:'大学生')
    asyncio.run(restarted.refresh_world(NOW + timedelta(minutes=1)))
    assert len(port.calls) == 1
    assert restarted.error_code == 'EMOTION_EVALUATION_UNAVAILABLE'
    asyncio.run(restarted.evaluate_received([receipt('new', '现在不用回复了。', NOW + timedelta(minutes=2))],
                                            now=NOW + timedelta(minutes=2)))
    assert len(port.calls) == 2


def test_three_sources_and_present_mood_use_one_bounded_http_packet(tmp_path, monkeypatch):
    import json
    from runtime.reply import jev_questions
    from runtime.reply.companion_decision import _json
    port = Decisions()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'life.sqlite3'), lambda: object(), lambda: '音乐专业大学生')
    records = [receipt(str(i), '我今天练琴有点累，需要休息。', NOW-timedelta(minutes=3-i)) for i in range(3)]
    view = asyncio.run(runtime.evaluate_received(records, now=NOW))
    assert runtime.error_code is None
    assert len(port.calls) == 1
    assert view['current_affect']['label'] == 'calm'
    assert len(view['reactions']) == 3
    state, questions, purpose = port.calls[0]
    assert len(questions) == 27  # includes affect_intensity
    assert state['sources']['s0']['preceding_sources'] == []
    assert state['sources']['s2']['preceding_sources'] == ['s0', 's1']
    assert all(set(s['context']) == {'as_of', 'rhythm'} for s in state['sources'].values())
    assert not {'world', 'projects', 'published_moments'} & set(state['current_affect'])
    body = _json(dict(state=state, questions=questions, purpose=purpose)).encode('utf-8')
    assert len(body) <= jev_questions.SEMANTIC_REQUEST_MAX_BYTES
    print('emotion_three_sources_http_bytes', len(body))
    # Already committed originals and unchanged mood must not call again.
    asyncio.run(runtime.evaluate_received(records, now=NOW))
    assert len(port.calls) == 1


def test_batch_cannot_resolve_concern_its_predecessor_did_not_open(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    class InvalidConcern(Decisions):
        async def ask(self, state, questions, *, purpose):
            answers = await super().ask(state, questions, purpose=purpose)
            assert not any('_existing_' in key for key in questions)
            answers['lifecycle_1'] = 'resolve_s0'
            return answers
    port = InvalidConcern()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'life.sqlite3'), lambda: object(), lambda: '学生')
    view = asyncio.run(runtime.evaluate_received([receipt('first', '我有点累。', NOW-timedelta(minutes=1)),
        receipt('next', '已经解决了。', NOW)], now=NOW))
    assert view['pending_current_input']
    assert runtime.error_code == 'EMOTION_EVALUATION_UNAVAILABLE'
    assert len(port.calls) == 1


def test_jev_stale_batch_is_not_retried_or_committed(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    class Changing(Decisions):
        async def ask(self, *args, **kwargs):
            result = await super().ask(*args, **kwargs)
            runtime.persona = lambda: 'changed persona'
            return result
    port = Changing()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'life.sqlite3'), lambda: object(), lambda: '学生')
    view = asyncio.run(runtime.evaluate_received([receipt()], now=NOW))
    assert view['pending_current_input']
    assert runtime.error_code == 'EMOTION_EVALUATION_UNAVAILABLE'
    assert len(port.calls) == 1


def test_batch_uses_one_real_http_exchange(tmp_path, monkeypatch):
    import hashlib
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from runtime.reply import jev_questions
    captured = []
    decisions = Decisions()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            captured.append((self.path, raw))
            packet = json.loads(raw)
            answers = asyncio.run(decisions.ask(packet['state'], packet['questions'], purpose=packet['purpose']))
            body = json.dumps({'backend': 'jev', 'input_digest': hashlib.sha256(raw).hexdigest(),
                               'decisions': answers}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = jev_questions.JevQuestionsPort(f'http://127.0.0.1:{server.server_port}/v1/companion/decide')
        monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
        monkeypatch.setattr(jev_questions, 'settle_receipt_sync', lambda *_: None)
        runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'life.db'), lambda: object(), lambda: '音乐专业大学生')
        records = [receipt(str(i), '今天练琴有些累。', NOW-timedelta(minutes=3-i)) for i in range(3)]
        view = asyncio.run(runtime.evaluate_received(records, now=NOW))
        assert runtime.error_code is None
        assert view['current_affect']['label'] == 'calm'
        assert len(captured) == 1
        assert captured[0][0] == '/v1/companion/semantic-decisions'
        assert len(captured[0][1]) <= jev_questions.SEMANTIC_REQUEST_MAX_BYTES
        print('emotion_captured_http_bytes', len(captured[0][1]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_published_source_sends_body_once_without_internal_json():
    import json
    from runtime.private_world.jev_emotion import appraise
    note = '练习遇到困难，先停下来休息。'
    envelope = dict(note=note, occurred_at=NOW.isoformat(), progress=[], source_id='world:1')
    source = dict(source_id='world:1', source_kind='published_world', occurred_at=NOW.isoformat(),
                  text=note+'\n'+json.dumps(envelope, ensure_ascii=False, separators=(',', ':')))
    packet = {'persona': '学生', 'assessment': {'sources': [source], 'source_contexts': {
        'world:1': {'concerns': [], 'prior_appraisals': []}}}}
    port = Decisions()
    result = asyncio.run(appraise(port, packet))
    assert port.calls[0][0]['sources']['s0']['source']['text'] == note
    assert result['appraisals'][0]['quote'] == note
    assert 'progress' in source['text']  # Canonical original is never modified.


def test_one_lifecycle_choice_opens_then_resolves_same_batch_without_phantom_concern(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    class Lifecycle(Decisions):
        async def ask(self, state, questions, *, purpose):
            answers = await super().ask(state, questions, purpose=purpose)
            assert not any('_existing_' in key for key in questions)
            answers['lifecycle_0'] = 'resolve_s1'
            return answers
    port = Lifecycle()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'life.db'), lambda: object(), lambda: '学生')
    view = asyncio.run(runtime.evaluate_received([receipt('pressure', '你必须一直练。', NOW-timedelta(minutes=1)),
        receipt('apology', '抱歉，我不再逼你练了，你休息吧。', NOW)], now=NOW))
    assert runtime.error_code is None
    assert not view['concerns']
    assert len(port.calls) == 1
    assert runtime.store.pending_source_ids(before=NOW) == []


def test_one_source_resolves_two_existing_concerns_and_opens_new_in_one_call(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    from tests.private_world.test_character_emotion import appraisal, commit
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'multi.db'), lambda: object(), lambda: '学生')
    for i in range(2):
        source_id = f'old-{i}'
        runtime.store.receive(source_id, '安排还没确认。', occurred_at=NOW-timedelta(minutes=3-i))
        commit(runtime.store, appraisal(source_id, '安排还没确认。', concern={
            'id': 'emotion:' + source_id, 'action': 'open', 'summary': '安排还没确认。'}))
    class Multiple(Decisions):
        async def ask(self, state, questions, *, purpose):
            answers = await super().ask(state, questions, purpose=purpose)
            answers.update(s0_existing_c0='resolve', s0_existing_c1='resolve', lifecycle_0='open')
            return answers
    port = Multiple()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    record = receipt('new', '两项安排都确认了，但是新的考试还没准备好。', NOW)
    view = asyncio.run(runtime.evaluate_received([record], now=NOW))
    assert runtime.error_code is None
    assert len(port.calls) == 1
    assert [item['id'] for item in view['concerns']] == ['emotion:' + record.source_id]
    with runtime.life_store._db() as db:
        import json
        item = json.loads(db.execute('SELECT payload FROM character_emotion_appraisals WHERE source_id=?', (record.source_id,)).fetchone()[0])
    assert len(item['concern']) == 3
    reopened = CharacterEmotionRuntime(runtime.life_store, lambda: object(), lambda: '学生')
    assert reopened.view(NOW)['concerns'] == view['concerns']


def test_two_batch_lifecycles_resolve_at_same_source_without_losing_either(tmp_path, monkeypatch):
    from runtime.reply import jev_questions
    class Multiple(Decisions):
        async def ask(self, state, questions, *, purpose):
            answers = await super().ask(state, questions, purpose=purpose)
            answers.update(lifecycle_0='resolve_s2', lifecycle_1='resolve_s2')
            return answers
    port = Multiple()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    runtime = CharacterEmotionRuntime(DailyLifeStore(tmp_path/'batch-multi.db'), lambda: object(), lambda: '学生')
    records = [receipt('one', '第一件事让我担心。', NOW-timedelta(minutes=2)),
               receipt('two', '第二件事也还没解决。', NOW-timedelta(minutes=1)),
               receipt('both', '两件事都解决了。', NOW)]
    view = asyncio.run(runtime.evaluate_received(records, now=NOW))
    assert runtime.error_code is None and not view['concerns']
    assert len(port.calls) == 1
    assert not runtime.store.pending_source_ids(before=NOW)
