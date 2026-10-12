"""Confidence diagnostics contain finite choices, never original exchange text."""
import asyncio
from copy import deepcopy
import json

from runtime.private_world.jev_exchange import extract


PRIVATE_TEXT = '我练完琴了。'
PRIVATE_REQUEST_ID = 'private-life-request-diagnostic'


class DiagnosticPort:
    def __init__(self):
        self.calls = []
        self.returned = []

    def choices(self, state, questions):
        result = {}
        for key, question in questions.items():
            options = question['criteria']
            choice = 'none' if 'none' in options else next(iter(options))
            if key == 'capacity':
                choice = 'ok'
            elif key == 'update_0_quote':
                choice = next(ref for ref, anchor in state['action_anchors'].items() if anchor['text'] == PRIVATE_TEXT)
            elif key == 'update_0_applicability_kind':
                choice = 'linli'
            elif key == 'update_0_status':
                choice = 'completed'
            elif key == 'update_0_evidence':
                choice = next(ref for ref, span in state['quotes'].items()
                              if ref in options and ref.startswith('r')
                              and state['sources']['linli_reply'][span[0]:span[1]] == PRIVATE_TEXT)
            result[key] = choice
        return result

    async def ask_detailed(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        answer = {}
        for key, choice in self.choices(state, questions).items():
            options = questions[key]['criteria']
            n = len(options)
            answer[key] = {'choice': choice, 'probabilities': {
                option: (.8 if n > 1 else 1.0) if option == choice else .2 / (n - 1) for option in options},
                'confidence': (.8 - 1 / n) / (1 - 1 / n) if n > 1 else 1.0,
                'confidence_source': 'upstream' if n > 1 else 'deterministic',
                'raw_exchange': PRIVATE_TEXT, 'billing': 'PRIVATE_BILLING_RECEIPT'}
        self.returned.append(deepcopy(answer))
        return answer


def _data():
    return {'user_letter': 'PRIVATE_USER_TEXT', 'linli_reply': PRIVATE_TEXT,
            'previous_state': {'projects': [{'id': 'PRIVATE_PROJECT_ID', 'title': 'PRIVATE_PROJECT_TITLE',
                                             'kind': 'linli'}]}}


def test_diagnostics_retain_both_phases_without_private_inputs_or_fact_fields():
    port = DiagnosticPort()
    diagnostics = []
    result = asyncio.run(extract(port, _data(), 'PRIVATE_INSTRUCTIONS', PRIVATE_REQUEST_ID,
                                 diagnostic_observer=diagnostics.append))
    assert [event['phase'] for event in diagnostics] == ['exchange-facts', 'exchange-anchored-facts']
    for event, (state, questions, purpose), raw in zip(diagnostics, port.calls, port.returned):
        assert set(event) == {'phase', 'decisions'} and event['phase'] == purpose
        assert set(event['decisions']) == set(questions)
        for key, detail in event['decisions'].items():
            assert set(detail) == {'choice', 'probabilities', 'confidence', 'confidence_source'}
            assert detail == {field: raw[key][field] for field in detail}
    encoded = json.dumps(diagnostics, ensure_ascii=False)
    for private in (PRIVATE_TEXT, PRIVATE_REQUEST_ID, 'PRIVATE_USER_TEXT', 'PRIVATE_PROJECT_ID',
                    'PRIVATE_PROJECT_TITLE', 'PRIVATE_INSTRUCTIONS', 'PRIVATE_BILLING_RECEIPT'):
        assert private not in encoded
    assert set(result) == {'updates', 'current_quote', 'relationship', 'routine', 'boundaries', 'addressing'}
    assert result['updates'][0]['status'] == 'completed'
    assert len(port.calls) == 2


def test_observer_mutation_cannot_change_extracted_choices_or_port_answers():
    port = DiagnosticPort()
    original = []

    def mutate(event):
        original.append(deepcopy(event))
        for detail in event['decisions'].values():
            detail['choice'] = 'none'
            detail['probabilities'].clear()
        event['decisions'].clear()

    result = asyncio.run(extract(port, _data(), '', PRIVATE_REQUEST_ID, diagnostic_observer=mutate))
    assert result['updates'][0]['status'] == 'completed'
    assert original[1]['decisions']['update_0_status']['choice'] == 'completed'
    assert port.returned[1]['update_0_status']['probabilities']['completed'] == .8


def test_legacy_port_is_observable_without_invented_confidence():
    base = DiagnosticPort()

    class Legacy:
        async def ask(self, state, questions, *, purpose):
            return base.choices(state, questions)

    diagnostics = []
    result = asyncio.run(extract(Legacy(), _data(), '', PRIVATE_REQUEST_ID,
                                 diagnostic_observer=diagnostics.append))
    assert result['updates'][0]['status'] == 'completed'
    assert len(diagnostics) == 2
    assert all(set(detail) == {'choice', 'confidence_source'} and detail['confidence_source'] == 'unavailable'
               for event in diagnostics for detail in event['decisions'].values())


def test_observer_is_optional_and_does_not_change_facts_or_request_count():
    plain, observed = DiagnosticPort(), DiagnosticPort()
    before = asyncio.run(extract(plain, _data(), '', PRIVATE_REQUEST_ID))
    diagnostics = []
    after = asyncio.run(extract(observed, _data(), '', PRIVATE_REQUEST_ID,
                                diagnostic_observer=diagnostics.append))
    assert before == after and len(plain.calls) == len(observed.calls) == 2
