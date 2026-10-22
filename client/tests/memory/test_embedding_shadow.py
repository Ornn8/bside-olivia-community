"""Cloud-vector facts, the shadow switch and the after-delivery shadow measurements (no network)."""
import math
from types import SimpleNamespace

import pytest

from runtime import cloud_policy
from runtime.diagnostics.reply_telemetry import take_shadow
from runtime.memory import remote_embedding
from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter as Adapter
from runtime.memory.source_retrieval import SourceRetrieval

WORDS = ['香菜', '豆子', '科目二', '晚安', '照片']


def vec(text):
    v = [1.0 if w in text else 0.0 for w in WORDS] + [0.2]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


@pytest.fixture
def index(tmp_path):
    return SourceRetrieval(tmp_path / 'original-text-index.sqlite3')


def test_fact_vectors_follow_the_stored_facts(index):
    facts = {'m1': '他不吃香菜', 'm2': '他的狗叫豆子'}
    missing = index.memory_vectors_missing('u', remote_embedding.MODEL, facts)
    assert {m for m, _, _ in missing} == {'m1', 'm2'}
    index.put_memory_vectors('u', remote_embedding.MODEL, [(m, d, vec(t)) for m, d, t in missing])
    assert index.memory_vector_coverage('u', remote_embedding.MODEL, facts) == 1.0
    assert index.memory_vectors_missing('u', remote_embedding.MODEL, facts) == []
    # An edited fact is embedded again; a forgotten one loses its vector.
    changed = {'m1': '他不吃香菜和葱'}
    assert [m for m, _, _ in index.memory_vectors_missing('u', remote_embedding.MODEL, changed)] == ['m1']
    assert index.memory_vector_coverage('u', remote_embedding.MODEL, {'m2': ''}) == 0.0
    hits = index.nearest_memories('u', remote_embedding.MODEL, vec('晚上吃面，别放香菜'), limit=2)
    assert hits[0][0] == 'm1'


def test_reply_vectors_keep_the_newest(index):
    for n in range(5):
        index.put_reply_vector('u', remote_embedding.MODEL, f'reply:{n}:1', vec('晚安' if n % 2 else '照片'), keep=3)
    assert len(index.recent_reply_vectors('u', remote_embedding.MODEL)) == 3
    assert len(index.recent_reply_vectors('u', remote_embedding.MODEL, exclude_source='reply:4:1')) == 2


@pytest.mark.parametrize('value,mode', [({'mode': 'on'}, 'on'), ({'mode': 'off'}, 'off'), ({}, 'shadow')])
def test_policy_switch(value, mode):
    assert cloud_policy.validate_policy({'embedding_memory': value})['embedding_memory'] == {'mode': mode}
    assert cloud_policy.validate_policy({})['embedding_memory'] == {'mode': 'shadow'}
    with pytest.raises(ValueError):
        cloud_policy.validate_policy({'embedding_memory': {'mode': 'maybe'}}, strict=True)


def stub(index, facts):
    records = {key: SimpleNamespace(memory_id=key, text=text, metadata={}, score=0.0) for key, text in facts.items()}
    return SimpleNamespace(_originals=index, _fact_records=lambda user: records, FACT_HITS=8, FACT_SCORE_FLOOR=0.55)


def test_shadow_lookup_never_waits_for_a_vector(index, monkeypatch):
    facts = {'m1': '他不吃香菜'}
    index.put_memory_vectors('u', remote_embedding.MODEL, [('m1', 'd', vec(facts['m1']))])
    monkeypatch.setattr(remote_embedding, 'available', lambda: True)
    monkeypatch.setattr(remote_embedding, 'embed_query', lambda text: pytest.fail('shadow must not call out'))
    monkeypatch.setattr(remote_embedding, 'cached_query', lambda text: None)
    monkeypatch.setattr('runtime.memory.mem0_memory.replace', lambda record, **kw: SimpleNamespace(**{**vars(record), **kw}))
    assert Adapter._fact_vector_hits(stub(index, facts), '晚上吃面', 'u', wait=False) == ()
    monkeypatch.setattr(remote_embedding, 'cached_query', lambda text: vec(text))
    hits = Adapter._fact_vector_hits(stub(index, facts), '晚上吃面，别放香菜', 'u', wait=False)
    assert [h.memory_id for h in hits] == ['m1'] and hits[0].metadata['retrieval_route'] == 'semantic_cloud'


def test_after_delivery_measures_repeats_and_echo(index, monkeypatch):
    take_shadow()
    monkeypatch.setattr(remote_embedding, 'available', lambda: True)
    monkeypatch.setattr(remote_embedding, 'embed', lambda texts, **kw: [vec(t) for t in texts])
    adapter = SimpleNamespace(_originals=index)
    Adapter._shadow_reply(adapter, 'u', 'reply:a:1', '我先睡了', '好，晚安。')
    assert 'shadow_reply_repeat' not in take_shadow()  # nothing to compare with yet
    Adapter._shadow_reply(adapter, 'u', 'reply:b:1', '晚安啦', '晚安，早点睡。')
    values = take_shadow()
    assert values['shadow_reply_repeat'] > 0.9 and values['shadow_reply_echo'] > 0.9
    monkeypatch.setattr(remote_embedding, 'embed', lambda texts, **kw: (_ for _ in ()).throw(OSError('offline')))
    Adapter._shadow_reply(adapter, 'u', 'reply:c:1', '在吗', '在。')  # never raises
