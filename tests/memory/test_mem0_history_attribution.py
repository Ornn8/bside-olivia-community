from types import SimpleNamespace

import pytest

from runtime.memory.mem0_history_attribution import bind_history_attribution


@pytest.mark.parametrize('actor', ['user', 'linli'])
@pytest.mark.parametrize('model_attribution', [None, 'user', 'linli', 'third-party'])
def test_insert_uses_historical_speaker_not_model_attribution(actor, model_attribution):
    calls = []
    class Store:
        def insert(self, vectors, payloads=None, ids=None):
            calls.append(payloads)
    backend = SimpleNamespace(vector_store=Store())
    bind_history_attribution(backend)
    bind_history_attribution(backend)
    payload = {'source_id': 'history:synthetic', 'domain': 'conversation_memory',
               'canonical': True, 'history_actor': actor, 'data': 'synthetic fact'}
    if model_attribution is not None:
        payload['attributed_to'] = model_attribution
    original = dict(payload)
    backend.vector_store.insert([[0.1]], [payload], ['id'])
    assert calls == [[dict(original, attributed_to=actor)]]
    assert payload == original


@pytest.mark.parametrize('change', [
    {'source_id': 'reply:synthetic'}, {'history_actor': 'third-party'},
    {'domain': 'other'}, {'canonical': False},
])
def test_insert_does_not_reattribute_noncanonical_or_unknown_speakers(change):
    calls = []
    backend = SimpleNamespace(vector_store=SimpleNamespace(insert=lambda **kwargs: calls.append(kwargs)))
    bind_history_attribution(backend)
    payload = dict({'source_id': 'history:synthetic', 'domain': 'conversation_memory',
                    'canonical': True, 'history_actor': 'linli', 'attributed_to': 'user'}, **change)
    backend.vector_store.insert(vectors=[], ids=[], payloads=[payload])
    assert calls[0]['payloads'] == [payload]


def test_insert_preserves_optional_payloads_and_ids():
    calls = []
    backend = SimpleNamespace(vector_store=SimpleNamespace(insert=lambda **kwargs: calls.append(kwargs)))
    bind_history_attribution(backend)
    backend.vector_store.insert([[0.1]], ids=['id'])
    assert calls == [{'vectors': [[0.1]], 'ids': ['id'], 'payloads': None}]
