"""One memory-extraction judgment for several exchanges (runtime.memory.extraction_bundle)."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from runtime import cloud_policy
from runtime.memory import extraction_bundle as bundle
from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
from runtime.memory.jev_memory_extraction import add_originals, candidate_spans, span_questions
from runtime.memory.mem0_memory import Mem0Config, Mem0ConversationMemoryAdapter, _HISTORY_USER_FACT_PROMPT
from runtime.reply import jev_questions

NOW = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
PROMPT = '保留带时态原文'


def row(source, user, reply, stamp='2026-11-08T03:00:00+00:00'):
    return {'source_id': source, 'digest': 'd-' + source, 'user_message': user, 'assistant_message': reply,
            'occurred_at': stamp}


def item(source, user, reply='好的。', rel=True):
    return {'source_id': source, 'user_message': user, 'rel': row(source, user, reply) if rel else None}


# ---------------------------------------------------------------- the request


def test_bundle_request_has_the_contracted_shape():
    state, questions, layout = bundle.build(
        [item('reply:a:1', '你好。明天去医院。', '路上小心。'), item('reply:b:1', '我不吃香菜。', '记住了。')],
        fact_prompt=PROMPT)
    assert set(state) == {'contract', 'memory_rules', 'original_messages', 'source_metadata', 'spans',
                          'exchanges', 'relationship_contract'}
    assert state['original_messages'] == [{'role': 'user', 'content': '你好。明天去医院。'},
                                          {'role': 'user', 'content': '我不吃香菜。'}]
    assert state['source_metadata'] == {'domain': 'conversation_memory', 'canonical': True, 'actor': 'local_user',
                                        'bundle': True, 'source_ids': ['reply:a:1', 'reply:b:1']}
    assert state['spans'] == {'s0': {'message_index': 0, 'start': 0, 'end': 3},
                              's1': {'message_index': 0, 'start': 3, 'end': 9},
                              's2': {'message_index': 1, 'start': 0, 'end': 6}}
    assert [e['source_id'] for e in state['exchanges']] == ['reply:a:1', 'reply:b:1']
    assert set(state['exchanges'][0]) == {'source_id', 'user_message', 'assistant_message', 'occurred_at'}
    assert set(questions) == {'s0', 's1', 's2', *(f'r{i}_{k}' for i in (0, 1) for k in ('identity', 'affection', 'agreement'))}
    assert questions['s1']['instructions'] == '遵守state.contract，判断state.spans.s1。'
    assert '遵守state.relationship_contract' in questions['r1_agreement']['instructions']
    assert 'state.exchanges[1]' in questions['r1_agreement']['instructions']
    assert all(set(q['criteria']) == {'keep', 'skip'} for q in questions.values())
    assert bundle.is_bundle(dict(state=state, questions=questions, purpose='memory-extraction'))
    assert not bundle.is_bundle(dict(state=state, questions=questions, purpose='decide'))


def test_without_a_pending_relationship_row_only_the_fact_part_is_asked():
    state, questions, layout = bundle.build([item('reply:a:1', '明天去医院。', rel=False)], fact_prompt=PROMPT)
    assert set(state) == {'contract', 'memory_rules', 'original_messages', 'source_metadata', 'spans'}
    assert set(questions) == {'s0'} and layout['exchanges'] == []


def test_the_single_message_request_is_unchanged():
    """add_originals still builds exactly the request released clients send."""
    sent = {}

    class Port:
        def ask_sync(self, state, questions, *, purpose):
            sent.update(state=state, questions=questions, purpose=purpose)
            return {key: 'skip' for key in questions}

    messages = [{'role': 'user', 'content': '如果明天下雨，我就不去。刚才说错了，是后天下午。'}]
    add_originals(Port(), SimpleNamespace(), messages, prompt=PROMPT, metadata={'source_id': 'reply:x:1'})
    assert list(sent['state']) == ['original_messages', 'spans', 'memory_rules', 'source_metadata', 'contract']
    assert sent['state']['contract'].startswith('按memory_rules判断原句') and sent['purpose'] == 'memory-extraction'
    assert sent['questions'] == {key: {'instructions': f'遵守state.contract，判断state.spans.{key}。',
                                       'criteria': {'keep': '保留这句带来源的原话供后续核对',
                                                    'skip': '没有足够具体或持续相关的信息'}}
                                 for key in ('s0', 's1')}
    assert not bundle.is_bundle(dict(state=sent['state'], questions=sent['questions'], purpose='memory-extraction'))


def test_answers_split_by_source_and_category():
    state, questions, layout = bundle.build(
        [item('reply:a:1', '你好。明天去医院。'), item('reply:b:1', '我不吃香菜。')], fact_prompt=PROMPT)
    answers = {key: 'skip' for key in questions}
    answers.update(s1='keep', s2='keep', r0_agreement='keep', r1_identity='keep', r1_affection='keep')
    kept, related = bundle.split(layout, answers)
    assert kept == {'reply:a:1': ['明天去医院。'], 'reply:b:1': ['我不吃香菜。']}
    assert [(r['source_id'], c) for r, c in related] == [('reply:a:1', ('agreement',)),
                                                           ('reply:b:1', ('identity', 'affection'))]


def test_kept_sentences_are_used_once_and_only_for_the_same_words():
    bundle.clear()
    bundle.put('reply:a:1', '你好。明天去医院。', ['明天去医院。'])
    assert bundle.take('reply:a:1', '别的内容。') is None          # other words: nothing, and the entry is spent
    bundle.put('reply:a:1', '你好。明天去医院。', ['明天去医院。'])
    assert bundle.take('reply:a:1', '你好。明天去医院。') == ['明天去医院。']
    assert bundle.take('reply:a:1', '你好。明天去医院。') is None   # once
    assert bundle.take(None, 'x') is None


def test_prejudged_sentences_replace_the_call_and_nothing_else():
    class Port:
        def ask_sync(self, *a, **k):
            raise AssertionError('no call')

    class Backend:
        def add(self, text, **options):
            self.text, self.options = text, options
            return {'results': [{'id': 'm1'}]}

    backend = Backend()
    result = add_originals(Port(), backend, [{'role': 'user', 'content': '你好。明天去医院。'}], prompt=PROMPT,
                           prejudged=['明天去医院。'], user_id='u', metadata={'source_id': 'reply:a:1'})
    assert result == {'results': [{'id': 'm1'}]}
    assert backend.text == '用户当时的原话（未经外部核实）：\n明天去医院。' and backend.options['infer'] is False
    # kept sentences that are not in the message are not trusted: the judgment is asked for as before
    asked = []

    class Asking:
        def ask_sync(self, state, questions, *, purpose):
            asked.append(purpose)
            return {key: 'skip' for key in questions}

    assert add_originals(Asking(), backend, [{'role': 'user', 'content': '你好。'}], prompt=PROMPT,
                         prejudged=['不是这条消息里的句子。']) == {'results': []}
    assert asked == ['memory-extraction']


# ---------------------------------------------------------------- grouping and waiting


def test_plan_groups_by_batch_and_kind_and_excludes_what_cannot_fit():
    items = [item(f'reply:{i}:1', '明天去医院。') for i in range(7)]
    bundles, excluded = bundle.plan(items, batch=3, fact_prompt=PROMPT)
    assert [len(b) for b in bundles] == [3, 3, 1] and excluded == []
    letter = item('reply:long:1', '很长的来信，' * 80 + '。')                  # over 400 characters, few sentences
    bundles, _ = bundle.plan([items[0], letter, items[1]], batch=6, fact_prompt=PROMPT)
    assert [[i['source_id'] for i in b] for b in bundles] == [['reply:0:1'], ['reply:long:1'], ['reply:1:1']]
    many = item('reply:many:1', '好。' * 65)                                  # 65 sentences: over the span capacity
    assert bundle.plan([many], batch=6, fact_prompt=PROMPT) == ([], [many])
    huge = item('reply:huge:1', '好。', 'x' * 12_001)
    assert bundle.plan([huge], batch=6, fact_prompt=PROMPT) == ([], [huge])
    bundles, excluded = bundle.plan(items[:4], batch=6, fact_prompt=PROMPT, fits_bytes=lambda state, questions: len(state['original_messages']) <= 2)
    assert [len(b) for b in bundles] == [2, 2] and excluded == []


@pytest.mark.parametrize('ages,expected', [
    ([], 0),
    ([10, 20], 0),                      # two fresh ones wait
    ([10, 20, 30, 40, 50, 60], 6),      # a full batch
    ([700, 800], 2),                    # quiet for longer than the idle time
    ([5, 1900], 2),                     # the oldest has waited three times the idle time
    ([5] * 100, 48),                    # bounded per scan
])
def test_flush_count(ages, expected):
    assert bundle.flush_count(ages, 6, 600) == expected


def test_policy_default_validation_and_fallback():
    assert cloud_policy.DEFAULTS['memory_extraction'] == {'mode': 'separate', 'batch': 6, 'idle_seconds': 600}
    assert cloud_policy.validate_policy({'memory_extraction': {'mode': 'bundle', 'batch': 4, 'idle_seconds': 300}},
                                        strict=True)['memory_extraction'] == {'mode': 'bundle', 'batch': 4, 'idle_seconds': 300}
    with pytest.raises(ValueError):
        cloud_policy.validate_policy({'memory_extraction': {'mode': 'sideways'}}, strict=True)
    with pytest.raises(ValueError):
        cloud_policy.validate_policy({'memory_extraction': {'batch': 7}}, strict=True)
    # a released-client style non-strict read ignores what it cannot use and unknown keys
    assert cloud_policy.validate_policy({'memory_extraction': {'mode': 'sideways'}, 'unknown': 1})['memory_extraction']['mode'] == 'separate'
    assert bundle.policy() == bundle.DEFAULT_POLICY


def test_bundle_request_names_its_price_version_and_others_do_not():
    sent = []

    class Transport:
        endpoint = 'http://127.0.0.1:8097/v1/companion/decide'

        def request_headers(self, digest):
            return {'X-Olivia-JEV-Price': 'jev-input-cny-20261027-v7'}

        def json_response(self, request):
            sent.append(dict(request.header_items()))
            raise ValueError('stop here')

    port = jev_questions.JevQuestionsPort('http://127.0.0.1:8097/v1/companion/decide')
    port.transport = Transport()
    state, questions, _ = bundle.build([item('reply:a:1', '明天去医院。')], fact_prompt=PROMPT)
    for packet in (dict(state=state, questions=questions, purpose='memory-extraction'),
                   dict(state={'original_messages': [], 'spans': {}}, questions=questions, purpose='memory-extraction')):
        with pytest.raises(ValueError):
            port._request(packet)
    versions = [headers.get('X-olivia-jev-price') for headers in sent]
    assert versions == [bundle.BUNDLE_PRICE_VERSION, 'jev-input-cny-20261027-v7']


# ---------------------------------------------------------------- the outbox end to end


class Port:
    """Keeps a sentence that mentions tomorrow, says yes to the agreement question only."""

    def __init__(self, fail_bundles=False):
        self.calls, self.fail_bundles = [], fail_bundles

    def request_size_bytes(self, state, questions, *, purpose):
        return len(json.dumps(dict(state=state, questions=questions), ensure_ascii=False).encode())

    def ask_sync(self, state, questions, *, purpose):
        kind = 'bundle' if bundle.is_bundle(dict(state=state, questions=questions, purpose=purpose)) else (
            'relationship' if 'exchanges' in state and 'spans' not in state else 'fact')
        self.calls.append((kind, state, questions))
        if kind == 'bundle' and self.fail_bundles:
            raise ValueError('JEV_UNAVAILABLE')
        answers = {}
        for key in questions:
            if key.startswith('s'):
                ref = state['spans'][key]
                sentence = state['original_messages'][ref['message_index']]['content'][ref['start']:ref['end']]
                answers[key] = 'keep' if '明天' in sentence else 'skip'
            else:
                answers[key] = 'keep' if key.endswith('agreement') else 'skip'
        return answers

    def kinds(self):
        return [kind for kind, _, _ in self.calls]


class Backend:
    def __init__(self):
        self.rows, self.added = [], []

    def add(self, text, **options):
        self.added.append((text, options['metadata']['source_id']))
        row = {'id': f'm{len(self.rows) + 1}', 'memory': text, 'user_id': options.get('user_id'),
               'agent_id': options.get('agent_id'), 'metadata': options['metadata'],
               'created_at': NOW.isoformat()}
        self.rows.append(row)
        return {'results': [{'id': row['id'], 'memory': text, 'event': 'ADD'}]}

    def get_all(self, **kwargs):
        wanted = kwargs['filters'].get('source_id')
        return {'results': [r for r in self.rows if wanted is None or r['metadata'].get('source_id') == wanted]}


def harness(tmp_path, monkeypatch, *, count, age_seconds, mode='bundle', batch=3, fail_bundles=False):
    backend, port = Backend(), Port(fail_bundles)
    adapter = Mem0ConversationMemoryAdapter(backend, Mem0Config(
        enabled=True, data_root=tmp_path / 'memory', llm_base_url='http://127.0.0.1:9/v1', llm_model='fixture',
        embedding_cache=tmp_path / 'models'))
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    monkeypatch.setattr(bundle, 'policy', lambda: {'mode': mode, 'batch': batch, 'idle_seconds': 600})
    bundle.clear()
    now = datetime.now(timezone.utc)
    letters = [dict(letter_id=f'chat{i}', reply_revision=1, letter_status='COMPLETED',
                    content=f'你好{i}。明天去医院{i}。', reply_text=f'路上小心{i}，我答应陪你。',
                    private_world_occurred_at=(now - timedelta(seconds=age_seconds - i)).isoformat())
               for i in range(count)]
    state, journal = tmp_path / 'letters.json', tmp_path / 'outbox.sqlite3'
    state.write_text(json.dumps({'letters': letters}), encoding='utf-8')
    outbox = CanonicalMemoryOutbox(state, journal, ConversationMemoryDeliveryCommitter(adapter))
    return adapter, backend, port, outbox


def scan(outbox):
    return asyncio.run(outbox.scan_once())


def labels(adapter):
    import sqlite3
    with sqlite3.connect(adapter._originals.path) as db:
        return dict(db.execute('SELECT source,categories FROM relationship_quotes').fetchall())


def test_a_full_batch_is_judged_in_one_request_and_written_as_before(tmp_path, monkeypatch):
    adapter, backend, port, outbox = harness(tmp_path, monkeypatch, count=3, age_seconds=30)
    result = scan(outbox)
    assert port.kinds() == ['bundle']
    _, state, questions = port.calls[0]
    assert len(state['original_messages']) == 3 and len(state['exchanges']) == 3
    assert result.delivered == 3
    assert sorted(source for _, source in backend.added) == ['reply:chat0:1', 'reply:chat1:1', 'reply:chat2:1']
    assert all('明天去医院' in text for text, _ in backend.added)
    assert labels(adapter) == {f'reply:chat{i}:1': '["agreement"]' for i in range(3)}
    scan(outbox)
    assert port.kinds() == ['bundle']                      # nothing more to judge


def test_fresh_exchanges_wait_for_their_batch_but_the_raw_index_has_them(tmp_path, monkeypatch):
    adapter, backend, port, outbox = harness(tmp_path, monkeypatch, count=2, age_seconds=30)
    result = scan(outbox)
    assert port.calls == [] and backend.added == [] and result.delivered == 0
    assert result.status == 'available'                    # waiting is not a failure
    found = adapter._originals.search('明天去医院', 'local-user', limit=5)
    assert {r.source_id for r in found} >= {'reply:chat0:1', 'reply:chat1:1'}
    # a third message fills the batch
    state = json.loads((tmp_path / 'letters.json').read_text('utf-8'))
    state['letters'].append(dict(state['letters'][0], letter_id='chat9', content='再来。明天去医院9。'))
    (tmp_path / 'letters.json').write_text(json.dumps(state), encoding='utf-8')
    assert scan(outbox).delivered == 3 and port.kinds() == ['bundle']


def test_a_quiet_conversation_is_judged_without_waiting_for_more(tmp_path, monkeypatch):
    adapter, backend, port, outbox = harness(tmp_path, monkeypatch, count=2, age_seconds=2000)
    assert scan(outbox).delivered == 2
    assert port.kinds() == ['bundle'] and len(backend.added) == 2


def test_separate_mode_is_the_former_two_judgments(tmp_path, monkeypatch):
    adapter, backend, port, outbox = harness(tmp_path, monkeypatch, count=2, age_seconds=30, mode='separate')
    assert scan(outbox).delivered == 2
    assert 'bundle' not in port.kinds()
    assert port.kinds().count('fact') == 2 and port.kinds().count('relationship') >= 1
    assert len(backend.added) == 2 and set(labels(adapter).values()) == {'["agreement"]'}


def test_a_failing_bundle_never_holds_memory_back(tmp_path, monkeypatch):
    adapter, backend, port, outbox = harness(tmp_path, monkeypatch, count=3, age_seconds=5000, fail_bundles=True)
    assert scan(outbox).delivered == 3                     # the bundle fails; the same scan writes them as before
    assert port.kinds()[0] == 'bundle' and port.kinds().count('fact') == 3
    assert len(backend.added) == 3
    scan(outbox)
    assert port.kinds().count('bundle') == 1               # nothing is retried
    assert set(labels(adapter).values()) == {'["agreement"]'}    # old enough for the former relationship path


def test_a_failing_bundle_leaves_fresh_relationship_rows_to_the_former_path_after_the_longest_wait(tmp_path, monkeypatch):
    adapter, backend, port, outbox = harness(tmp_path, monkeypatch, count=3, age_seconds=30, fail_bundles=True)
    # a full batch is due at once, the bundle fails, the facts are written by the former path in the same scan
    assert scan(outbox).delivered == 3 and len(backend.added) == 3
    assert set(labels(adapter).values()) == {None}         # the bundle still owns the fresh relationship rows ...
    older = datetime.now(timezone.utc) + timedelta(seconds=4 * 600 + 5)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return older if tz else older.replace(tzinfo=None)
    monkeypatch.setattr('runtime.memory.mem0_memory.datetime', Later)
    scan(outbox)
    assert set(labels(adapter).values()) == {'["agreement"]'}   # ... until they are older than its longest wait
