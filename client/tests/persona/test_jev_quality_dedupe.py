"""Synthetic request equivalence and serialized size regression checks."""
import asyncio
from copy import deepcopy
import json

import pytest

from runtime.reply import jev_quality as quality
from runtime.reply.companion_decision import _json
from runtime.reply.reply_model_quality import _LayerAuthority, _LAYER_SPECS
from runtime.reply.reply_review_policy import FACT_REVIEW_SCOPE


FACT_SUFFIX = (
    '有明确具体事实冲突或无支持选unsupported；没有具体事实主张选none。'
    '得到支持须选择fact_sources_ref中的具体eN，并检查其说话人、时间、类型与全文含义；'
    '多项事实须各有本层来源支持，所选eN是主要来源，不能用一个真事实掩盖另一个编造。'
    '当前提问预设不证明记得，转写不证明听见，角色旧说法只证明说过。'
    'story/asmr_story允许故事范围内虚构；对真实用户的听觉、经历等断言仍须证据。'
    '普通情绪、比喻、条件、提问不是已发生事实。'
)


def synthetic_requests(variant):
    candidate = ''.join(f'Synthetic sentence {i}.\n' for i in range(20))
    packet = {'kind': 'relationship_history', 'coverage': 'bounded', 'records': [
        {'citation': f'event:{i}', 'speaker': 'character', 'text': f'SYNTHETIC_RECORD_{i} ' * 12,
         'occurred_at': '2026-10-01T10:00:00Z', 'evidence_scope': 'recorded_utterance'}
        for i in range(4)]}
    other = deepcopy(packet)
    if variant == 'different_records':
        other['records'][0]['text'] = 'Different original utterance'
    if variant == 'different_metadata':
        other['coverage'] = 'another bounded window'
    wrapper = {'untrusted': True, 'fragment_id': 'synthetic.relationship', 'text': json.dumps(packet)}
    data = {'candidate_reply': candidate, 'mode': 'text_letter', 'current_user_input': 'Synthetic question',
            'relationship_context': {'stage': 'acquaintance'}, 'relationship_history': [other],
            'memory_evidence': {'assembled_memory': '<untrusted_history>' + json.dumps(wrapper) + '</untrusted_history>'}}
    if variant == 'absent':
        data.pop('relationship_history')
        data['memory_evidence'] = {}
    requests = [(_LayerAuthority(name, spec['question'], spec['codes'], '', '', ''),
                 ({'content': ''}, {'content': json.dumps(data)})) for name, spec in _LAYER_SPECS.items()]
    return candidate, requests


def resolve(value, catalog):
    if isinstance(value, dict):
        if set(value) == {'catalog_ref', 'path'}:
            target = catalog[value['catalog_ref']]
            for part in value['path']:
                target = target[part]
            return resolve(target, catalog)
        return {key: resolve(item, catalog) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, catalog) for item in value]
    return value


class Capture:
    def __init__(self):
        self.calls = []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((deepcopy(state), deepcopy(questions), purpose))
        answers = {}
        for key, question in questions.items():
            choices = question['criteria']
            answers[key] = next((item for item in ('none', 'no', 'current', 'n') if item in choices), next(iter(choices)))
        return answers


@pytest.mark.parametrize('variant', ['same', 'different_records', 'different_metadata', 'absent'])
def test_deduped_request_preserves_every_layer_and_question(variant, monkeypatch):
    candidate, requests = synthetic_requests(variant)
    port = Capture()
    asyncio.run(quality.review_layers_json(port, requests, candidate, False))
    state, questions, purpose = port.calls[0]
    monkeypatch.setattr(quality, '_dedupe_review_request', lambda state, questions: (state, questions))
    baseline = Capture()
    asyncio.run(quality.review_layers_json(baseline, requests, candidate, False))
    old_state, old_questions, _ = baseline.calls[0]
    assert questions.keys() == old_questions.keys()
    for key, question in questions.items():
        assert question['criteria'] == old_questions[key]['criteria']
        if ':fact:' not in key:
            assert question == old_questions[key]
    assert len(port.calls) == 1 and purpose == 'quality-review'
    assert 'candidate' not in state
    assert list(state['catalog'].values()).count(candidate) == 1
    assert state['fact_question_rules'] == FACT_REVIEW_SCOPE + FACT_SUFFIX
    expected_catalog, expected_layers = {}, {}

    def ref(value):
        for key, item in expected_catalog.items():
            if item == value:
                return key
        key = f'v{len(expected_catalog)}'
        expected_catalog[key] = value
        return key

    for layer, messages in requests:
        original = quality._purpose_state(layer, messages, {})
        actual = state['layers'][layer.name]
        assert actual['rules'] == original['rules']
        assert {key: resolve(state['catalog'][value], state['catalog'])
                for key, value in actual['input_refs'].items()} == original['input']
        expected_layers[layer.name] = {'rules': original['rules'],
                                     'input_refs': {key: ref(value) for key, value in original['input'].items()}}
        if 'MEMORY_FABRICATION' in layer.allowed_codes:
            sources = quality._fact_sources(original['input'])
            expected_layers[layer.name]['fact_sources_ref'] = ref(sources)
            assert state['catalog'][actual['fact_sources_ref']] == sources
            for sid in state['spans']:
                lid = next(key for key, name in state['layer_refs'].items() if name == layer.name)
                question = questions[f'{lid}:fact:{sid}']
                assert question['instructions'] == f'{lid}：核对{sid}，按state.fact_question_rules判断。'
                assert question['criteria'] == {'none': '没有需要核实的现实事实',
                    'unsupported': '存在不受支持的现实断言', **{key: key for key in sources}}

    legacy = {key: value for key, value in state.items() if key not in {'fact_question_rules', 'reference_contract'}}
    legacy.update(candidate=candidate, catalog=expected_catalog, layers=expected_layers)
    legacy_questions = deepcopy(questions)
    for key, question in legacy_questions.items():
        if ':fact:' in key:
            lid, _, sid = key.split(':')
            question['instructions'] = f'{lid}：核对{sid}。' + FACT_REVIEW_SCOPE + FACT_SUFFIX
            assert question['instructions'] == f'{lid}：核对{sid}。' + state['fact_question_rules']
    assert legacy == old_state
    assert legacy_questions == old_questions
    before = len(_json(dict(state=legacy, questions=legacy_questions, purpose=purpose)).encode('utf-8'))
    after = len(_json(dict(state=state, questions=questions, purpose=purpose)).encode('utf-8'))
    assert after < before
    if variant in {'same', 'different_metadata'}:
        assert _json(state).count('SYNTHETIC_RECORD_0') == 12
    print(f'{variant}: {before} -> {after} UTF-8 bytes')


def test_confirmation_packet_and_results_are_unchanged(monkeypatch):
    class Flagged(Capture):
        async def ask(self, state, questions, *, purpose):
            answers = await super().ask(state, questions, purpose=purpose)
            for key in questions:
                if key.endswith(':STAGE_DRIFT'):
                    answers[key] = 's0'
            return answers

    candidate, requests = synthetic_requests('same')
    compact = Flagged()
    result = asyncio.run(quality.review_layers_json(compact, requests, candidate, True))
    monkeypatch.setattr(quality, '_dedupe_review_request', lambda state, questions: (state, questions))
    original = Flagged()
    assert asyncio.run(quality.review_layers_json(original, requests, candidate, True)) == result
    assert len(compact.calls) == len(original.calls) == 2
    assert compact.calls[1] == original.calls[1]
    assert compact.calls[1][2] == 'quality-confirm'
