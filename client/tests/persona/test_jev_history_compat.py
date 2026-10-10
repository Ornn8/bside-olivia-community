"""Old history requests keep their signed identity while meeting Choice types."""
import copy
import hashlib
import json

import pytest

from runtime.reply.jev_semantic_service import decide


@pytest.mark.parametrize('purpose', ['historical-relationship', 'synthetic-other-purpose'])
def test_numeric_history_labels_are_normalized_only_in_provider_projection(purpose):
    criteria = {str(i): i for i in range(101)}
    packet = {'purpose': purpose, 'state': {'synthetic': True}, 'questions': {
        key: {'instructions': 'Synthetic assessment.', 'criteria': dict(criteria)}
        for key in ('familiarity', 'trust', 'comfort', 'closeness', 'tension')}}
    before = copy.deepcopy(packet)
    received = []

    class Provider:
        def ask(self, state, questions):
            received.append(questions)
            return {key: {'type': 'choice', 'choice': '20'} for key in questions}

    result = decide(Provider(), packet)
    expected = {key: str(value) for key, value in criteria.items()} if purpose == 'historical-relationship' else criteria
    assert all(question['criteria'] == expected for question in received[0].values())
    assert result['decisions'] == {key: '20' for key in packet['questions']}
    assert result['input_digest'] == hashlib.sha256(json.dumps(
        before, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')).hexdigest()
    assert packet == before


def test_compatibility_does_not_coerce_other_fields_or_invalid_scores():
    received = []
    questions = {
        'familiarity': {'instructions': 'Synthetic.', 'criteria': {'0': 0, '20': '20', '21': True, '22': 23, '101': 101}},
        'e1': {'instructions': 'Synthetic.', 'criteria': {'yes': 1, 'no': 0}},
    }

    class Provider:
        def ask(self, state, native):
            received.append(native)
            return {key: {'type': 'choice', 'choice': next(iter(value['criteria']))}
                    for key, value in native.items()}

    decide(Provider(), {'purpose': 'historical-relationship', 'state': {}, 'questions': questions})
    assert received[0]['familiarity']['criteria'] == {'0': '0', '20': '20', '21': True, '22': 23, '101': 101}
    assert received[0]['e1']['criteria'] == questions['e1']['criteria']
