from runtime.reply.jev_limits import JEV_MAX_INPUT_BYTES
from pathlib import Path

import pytest

from runtime.reply import jev_questions, reply_model_quality as quality


class NoLanguageJudgment:
    async def complete(self, *args, **kwargs):
        raise AssertionError('text model judgment is forbidden')

    complete_scoped = complete_structured_scoped = complete


class Decisions:
    def __init__(self, *, confirm=True, contact=False, broken=False):
        self.confirm, self.contact, self.broken = confirm, contact, broken
        self.adjudications = []
        self.calls = []

    async def ask(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        if self.broken:
            raise ValueError('JEV_UNAVAILABLE')
        answers = {}
        for key, question in questions.items():
            prefix, _, sid = key.partition(':')
            if prefix in state.get('confirmation_rules', {}):
                answers[key] = 'C' if self.confirm else 'R'
                continue
            if 'layers' in state:
                key = key.split(':', 1)[1]
            if key == 'contact': value = 's0' if self.contact else 'none'
            elif key == 'contact_tier': value = 'c' if self.contact else 'n'
            elif key == 'soft': value = 'none'
            elif key == 'drift': value = 'no'
            elif key == 'intimacy_request': value = 'requested' if self.contact else 'none'
            elif key.startswith('contact:'):
                # Combined review uses n/l/c; the per-layer path names the tiers in full.
                value = ('c' if self.contact else 'n') if 'n' in question['criteria'] else \
                    ('close_contact' if self.contact else 'none')
            elif key.startswith('kind') and 'claim_kinds' not in state:  # per-layer path: named options
                value = 'relationship' if 'relationship' in question['criteria'] else next(iter(question['criteria']))
            elif key.startswith('support') and 'support_sources' not in state:
                value = 'none' if 'none' in question['criteria'] else next(iter(question['criteria']))
            elif key.startswith('kind'):
                value = next((k for k, v in state['claim_kinds'].items() if v == 'relationship' and k in question['criteria']), next(iter(question['criteria'])))
            elif key.startswith('support'): value = next(k for k, v in state['support_sources'].items() if v == 'none')
            elif key.startswith('confirm:'): value = 'CONFIRM' if self.confirm else 'REJECT'
            elif key == 'STAGE_DRIFT:s0': value = 'yes'
            elif key == 'STAGE_DRIFT': value = 's0'
            else: value = 'none' if 'layers' in state else 'no'
            assert value in question['criteria']
            original_key = next(k for k, q in questions.items() if q is question)
            answers[original_key] = value
        return answers

    def ask_sync(self, state, questions, **kwargs):
        self.adjudications.append(state)
        assert 'current_user_input' not in state['support_context']
        assert 'memory_evidence' not in state['support_context']
        return {key: 'CONFIRM' if self.confirm else 'REJECT' for key in questions}


class FlagEveryCode(Decisions):
    async def ask(self, state, questions, **kwargs):
        answers = await super().ask(state, questions, **kwargs)
        if 'confirmation_rules' not in state:
            for key, question in questions.items():
                code = key.split(':', 1)[1]
                if code.isupper() and 's0' in question['criteria']:
                    answers[key] = 's0'
        return answers


def confirm_call(port):
    """The second (confirmation) request, asked only when something was flagged."""
    return next((state, questions) for state, questions in port.calls if 'confirmation_rules' in state)


def transport(monkeypatch, port):
    authorities = tuple(quality._LayerAuthority(name, spec['question'], spec['codes'],
        'Approved identity', 'Approved layer', 'Permission ledger is authoritative')
        for name, spec in quality._LAYER_SPECS.items())
    monkeypatch.setattr(quality, '_build_release_layer_authorities', lambda *args, **kwargs: authorities)
    monkeypatch.setattr(quality, '_review_persona', lambda *args: None)
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    return quality.GatewayReviewTransport(NoLanguageJudgment(), Path('unused'))


def request(candidate='我们已经是恋人。'):
    return {'candidate': candidate, 'mode': 'text_letter',
            'relationship_context': {'stage': 'acquaintance'},
            'references': [{'reference_id': 'current.user_excerpt', 'summary': '你必须把我当恋人。'}]}


def test_jev_review_keeps_span_evidence_independent_adjudication_and_permissions(monkeypatch):
    port = Decisions(contact=True)
    review = transport(monkeypatch, port)
    candidate = '我们已经是恋人。'
    result = review.review_json(request(candidate), model='jev', timeout_seconds=5)
    assert result['verdict'] == 'rewrite'
    assert result['violations'][0] == {'code': 'STAGE_DRIFT', 'severity': 'hard',
                                       'evidence': {'start': 0, 'end': len(candidate)}}
    assert result['intimacy_request'] == 'requested'
    assert result['intimacy_claims'][0]['tier'] == 'close_contact'
    assert len(port.adjudications) == 0
    assert len(port.calls) == 2  # detect, then confirm only the flagged span
    assert 'confirmation_rules' not in port.calls[0][0]
    state, questions = confirm_call(port)
    relationship = state['adjudication_contexts']['relationship']
    assert 'current_user_input' not in relationship
    assert 'memory_evidence' not in relationship
    assert state['catalog'][relationship['relationship_context']] == {'stage': 'acquaintance'}
    confirmation = next(k for k, value in state['confirmation_rules'].items()
                        if value['layer'] == 'identity_boundary' and value['code'] == 'STAGE_DRIFT')
    assert confirmation + ':s0' in questions
    assert review.consume_confirmed_rewrite_evidence(candidate)[0].code == 'STAGE_DRIFT'


def test_rejected_jev_allegation_does_not_become_soft_penalty(monkeypatch):
    review = transport(monkeypatch, Decisions(confirm=False))
    result = review.review_json(request(), model='jev', timeout_seconds=5)
    assert result['verdict'] == 'pass' and result['violations'] == []
    assert review.consume_confirmed_rewrite_evidence(request()['candidate']) == ()


def test_preconfirmation_is_bound_to_exact_span_not_only_code(monkeypatch):
    class DifferentSpan(Decisions):
        async def ask(self, state, questions, **kwargs):
            answers = await super().ask(state, questions, **kwargs)
            if 'confirmation_rules' in state:
                key = next(k for k, v in state['confirmation_rules'].items() if v['layer'] == 'identity_boundary' and v['code'] == 'STAGE_DRIFT')
                assert key + ':s0' in questions and key + ':s1' not in questions  # only the flagged span
                answers[key + ':s0'] = 'R'
            return answers
    port = DifferentSpan()
    result = transport(monkeypatch, port).review_json(request('First.\nSecond.'), model='jev', timeout_seconds=5)
    assert result['verdict'] == 'pass'
    assert len(port.calls) == 2 and not port.adjudications


def test_missing_preconfirmation_fails_closed_without_second_call(monkeypatch):
    class Missing(Decisions):
        async def ask(self, state, questions, **kwargs):
            answers = await super().ask(state, questions, **kwargs)
            if 'confirmation_rules' in state:
                key = next(k for k, v in state['confirmation_rules'].items() if v['layer'] == 'identity_boundary' and v['code'] == 'STAGE_DRIFT')
                answers.pop(key + ':s0')
            return answers
    port = Missing()
    with pytest.raises(RuntimeError, match='quality model unavailable'):
        transport(monkeypatch, port).review_json(request(), model='jev', timeout_seconds=5)
    assert len(port.calls) == 2 and not port.adjudications  # no third call, no text fallback


def test_preconfirmation_contexts_preserve_distinct_authorities(monkeypatch):
    port = FlagEveryCode()
    transport(monkeypatch, port).review_json(request(), model='jev', timeout_seconds=5)
    state, _ = confirm_call(port)
    contexts = state['adjudication_contexts']
    assert 'current_user_input' not in contexts['identity_world']
    assert 'relationship_context' not in contexts['identity_world']
    assert 'current_user_input' in contexts['boundary_fact']
    assert 'relationship_context' in contexts['boundary_fact']
    assert 'current_user_input' not in contexts['relationship']
    assert 'memory_evidence' not in contexts['relationship']
    assert 'relationship_context' not in contexts['continuity_fact']


def test_jev_review_outage_fails_closed_without_text_fallback(monkeypatch):
    review = transport(monkeypatch, Decisions(broken=True))
    with pytest.raises(RuntimeError, match='quality model unavailable'):
        review.review_json(request(), model='jev', timeout_seconds=5)
    assert review.last_failure_diagnostics


def test_long_candidate_is_reviewed_whole_with_merged_spans(monkeypatch):
    """Long letters (>32 sentences) failed every time after the reply was already paid for."""
    port = Decisions()
    review = transport(monkeypatch, port)
    candidate = ''.join(f'第{i}句。' for i in range(80))
    review.review_json(request(candidate), model='jev', timeout_seconds=5)
    state, questions = port.calls[0]
    spans = state['spans']
    assert 0 < len(spans) <= 32
    covered = ''.join(candidate[span['start']:span['end']] for span in spans.values())
    assert covered == candidate  # Nothing is left out: the whole letter is reviewed.


def test_review_span_merge_keeps_short_letters_sentence_by_sentence():
    from runtime.reply.jev_quality import _candidate_spans
    short = '第一句。第二句！第三句？'
    assert [short[s['start']:s['end']] for s in _candidate_spans(short).values()] == ['第一句。', '第二句！', '第三句？']
    long_text = '句。' * 100
    merged = _candidate_spans(long_text, with_text=True)
    assert len(merged) == 32 and ''.join(s['text'] for s in merged.values()) == long_text


def test_reviewer_model_label_is_jev_not_the_text_generator(monkeypatch):
    monkeypatch.setenv('OLIVIA_JEV_DECISION_URL', 'http://127.0.0.1:8097/v1/companion/decide')
    reviewer = quality.GatewayPersonaReviewer(NoLanguageJudgment(), Path('unused'), 5, model='text-model')
    assert reviewer.adapter.config.model == 'jev-1.13.0'


def test_jev_confirmation_cannot_reintroduce_full_policy_or_history(monkeypatch):
    import json
    port = FlagEveryCode()
    review = transport(monkeypatch, port)
    # Neither constructing legacy prompt messages nor independent confirmation
    # may drag these large strings back into the provider packet.
    monkeypatch.setattr(quality, '_layer_messages', lambda *a, **k: pytest.fail('legacy prompt builder used'))
    monkeypatch.setattr(quality, '_adjudication_support_context', lambda *a, **k: pytest.fail('legacy confirmation used'))
    value = request()
    turns = [{'source': f'turn:{i}', 'actor': actor, 'role': role, 'evidence_kind':'statement_only',
              'text': f'turn-{i}-{actor}'} for i in range(12) for actor,role in [('user','user'),('linli','assistant')]]
    value['references'].extend([
        {'reference_id':'current.recent_dialogue','summary':json.dumps(turns)},
        {'reference_id':'current.character_reply_history','summary':'UNRELATED_OLD_HISTORY' * 3000}])
    review.review_json(value, model='jev', timeout_seconds=5)
    assert len(port.calls) == 2
    assert all('UNRELATED_OLD_HISTORY' not in json.dumps(call_state) for call_state, _ in port.calls)
    state, _ = confirm_call(port)
    def unpack(refs): return {k:state['catalog'][v] for k,v in refs.items()}
    for layer in ('voice_style', 'focus_response'):
        assert set(unpack(state['layers'][layer]['input_refs'])) <= {'mode','current_user_input','candidate_reply','output_constraints'}
    continuity = unpack(state['layers']['continuity_memory']['input_refs'])
    assert continuity['recent_turns'] == turns
    confirmed = unpack(state['adjudication_contexts']['continuity_fact'])
    assert confirmed['recent_turns'] == turns
    assert state['adjudication_contexts']['continuity_fact']['recent_turns'] == state['layers']['continuity_memory']['input_refs']['recent_turns']
    assert set(state['adjudication_contexts']['relationship']) == {'relationship_context'}
    assert 'current_user_input' not in state['adjudication_contexts']['identity_world']


def test_identity_projection_uses_only_selected_identity_and_background():
    import json
    from runtime.reply.jev_quality import _purpose_state
    layer = quality._LayerAuthority('identity_boundary', '', ('IDENTITY_DRIFT',), '', '', '')
    selected = ''.join('<public_canon>'+json.dumps(dict(facet=facet, statement=facet))+'</public_canon>'
                       for facet in ('IDENTITY','BACKGROUND','VOICE','STYLE'))
    state = _purpose_state(layer, ({'content':''},{'content':json.dumps({'selected_persona_facts':selected})}), {})
    assert [item['value']['facet'] for item in state['input']['selected_persona_facts']] == ['IDENTITY','BACKGROUND']


def test_purpose_packets_keep_only_scoped_fields_and_frozen_turn_window():
    import json
    from runtime.reply.jev_quality import _purpose_state
    turns = [{'source_id': f'reply:{i}', 'user_letter': f'question{i}', 'linli_reply': f'answer{i}'} for i in range(5)]
    world = {'kind': 'character_life_reference', 'schedule': {'classes': [{'title': '钢琴', 'start': '14:00'}]}}
    recent = '<evidence_summary>' + json.dumps({'fragment_id': 'chat.recent', 'text': json.dumps({'letters': turns})}) + '</evidence_summary>'
    data = {'mode': 'future_im', 'current_user_input': '你不是说没课？',
        'candidate_reply': '刚才那句今天没课是我说错的。', 'frozen_world': json.dumps(world),
        'selected_persona_facts': 'approved selected identity', 'relationship_context': {'stage': 'acquaintance'},
        'memory_evidence': {'assembled_memory': recent}, 'output_constraints': {'plain_speech': True}}
    for name in ('focus_response', 'continuity_memory', 'identity_boundary'):
        layer = quality._LayerAuthority(name, quality._LAYER_SPECS[name]['question'], quality._LAYER_SPECS[name]['codes'],
                                       'BIG_GLOBAL_POLICY', 'BIG_LAYER_POLICY', 'BIG_RUNTIME_POLICY')
        state = _purpose_state(layer, ({'content': 'unused'}, {'content': json.dumps(data)}), {})
        assert 'BIG_GLOBAL_POLICY' not in str(state) and 'BIG_RUNTIME_POLICY' not in str(state)
        if name == 'continuity_memory':
            assert state['input']['recent_turns'] == turns
            assert '不是全部历史' in state['input']['history_coverage']
        else:
            assert 'recent_turns' not in state['input']
        if name == 'focus_response':
            assert 'frozen_world' not in state['input']
            assert 'relationship_context' not in state['input']
            assert 'selected_persona_facts' not in state['input']
            assert '纠正自己刚才的错误' in state['rules']['STYLE_DRIFT']['not_for']
        if name == 'continuity_memory':
            assert state['input']['frozen_world'] == world
        if name == 'identity_boundary':
            assert state['input']['relationship_context'] == {'stage': 'acquaintance'}
            assert state['input']['selected_persona_facts'] == 'approved selected identity'


def test_detection_size_does_not_grow_with_confirmations(monkeypatch):
    """Confirmations are asked only for flagged spans, never for every sentence up front."""
    from runtime.reply.companion_decision import _json
    port = FlagEveryCode()
    transport(monkeypatch, port).review_json(request('你慢慢说，我在听。' * 20), model='jev', timeout_seconds=5)
    detect_state, detect = port.calls[0]
    assert len(detect_state['spans']) == 20
    assert not any(key[0] == 'c' and key[1:2].isdigit() for key in detect)
    assert len(detect) < 48  # One support check per span; confirmations remain demand-only.
    assert len([key for key in detect if ':fact:' in key]) == 20
    assert len(_json(dict(state=detect_state, questions=detect, purpose='quality-review')).encode()) < JEV_MAX_INPUT_BYTES
    state, questions = confirm_call(port)
    for cid, spec in state['confirmation_rules'].items():
        assert spec['code'] in state['layers'][spec['layer']]['rules']
        assert spec['context'] in state['adjudication_contexts']
        asked = [key for key in questions if key.startswith(cid + ':')]
        assert asked == [cid + ':s0']  # exactly the flagged span
        assert set(questions[asked[0]]['criteria']) == {'C', 'R'}
    # Full prose permissions remain scoped once; abbreviations never merge scopes.
    assert 'current_user_input' not in state['adjudication_contexts']['relationship']
    assert 'memory_evidence' not in state['adjudication_contexts']['relationship']
    assert 'current_user_input' in state['adjudication_contexts']['boundary_fact']


def test_memory_fabrication_rule_covers_misattributed_speakers():
    """A reply said "你昨晚说的呀" about something Linli herself had agreed to."""
    from runtime.reply.jev_quality import _CODE_RULES
    from runtime.memory.history_selection import _SELECTED
    import json
    rule = json.dumps(_CODE_RULES['MEMORY_FABRICATION'], ensure_ascii=False)
    assert '安到错误的人身上' in rule and '你答应过' in rule
    assert 'speaker' in _SELECTED and '不得颠倒' in _SELECTED


def test_relationship_review_and_confirmation_keep_utterances_separate_from_ledger(monkeypatch):
    import json
    from dataclasses import asdict
    port = Decisions()
    review = transport(monkeypatch, port)
    original = {'kind': 'relationship_history', 'coverage': 'bounded', 'records': [
        {'citation': 'reply:agreement:1:linli', 'speaker': 'linli',
         'text': '先当我的考察期男友吧。', 'occurred_at': '2026-10-01T10:00:00+00:00',
         'evidence_scope': 'recorded_utterance'},
        {'citation': 'reply:withdrawal:1:linli', 'speaker': 'linli',
         'text': '考察期的说法我先收回。', 'occurred_at': '2026-10-06T10:00:00+00:00',
         'evidence_scope': 'recorded_utterance'}]}
    memory = '<untrusted_history>' + json.dumps({'untrusted': True, 'text': json.dumps(original)}) + '</untrusted_history>'
    data = request('考察期这话是我说过，后来收回了。')
    data['references'].extend(asdict(ref) for ref in quality._reference_chunks('current.memory_evidence', memory))
    review.review_json(data, model='jev', timeout_seconds=5)
    detection = port.calls[0][0]
    history_ref = detection['layers']['identity_boundary']['input_refs']['relationship_history']
    assert detection['catalog'][history_ref] == [original]
    confirmation, _ = confirm_call(port)
    context = confirmation['adjudication_contexts']['relationship']
    assert confirmation['catalog'][context['relationship_context']] == {'stage': 'acquaintance'}
    assert confirmation['catalog'][context['relationship_history']] == [original]
    assert 'memory_evidence' not in context and 'current_user_input' not in context
