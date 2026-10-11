from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime.memory import remote_embedding
from runtime.memory.source_retrieval import SourceRetrieval

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(remote_embedding, '_failed_at', -1e9)
    monkeypatch.setattr(remote_embedding, '_queries', {})
    monkeypatch.setattr(remote_embedding, '_pending', {})
    monkeypatch.setattr(remote_embedding, '_get_key', lambda: 'olivia-synthetic')
    monkeypatch.delenv('OLIVIA_REMOTE_EMBEDDING', raising=False)
    calls = []

    def post(key, texts, timeout):
        calls.append((key, list(texts), timeout))
        return [[1.0, 0.0] if '海' in text else [0.0, 1.0] for text in texts]
    monkeypatch.setattr(remote_embedding, '_post', post)
    return calls


def test_needs_an_olivia_key_and_can_be_switched_off(monkeypatch, fresh):
    for key in (None, '', 'sk-other', 'olivia-'):
        monkeypatch.setattr(remote_embedding, '_get_key', lambda key=key: key)
        assert remote_embedding.embed(['x']) is None
    monkeypatch.setattr(remote_embedding, '_get_key', lambda: ' olivia-synthetic\n')
    assert remote_embedding.embed(['看海']) == [[1.0, 0.0]]
    assert fresh[-1][0] == 'olivia-synthetic'
    monkeypatch.setenv('OLIVIA_REMOTE_EMBEDDING', '0')
    assert remote_embedding.embed(['看海']) is None and not remote_embedding.available()


def test_batches_and_cuts_long_texts(fresh):
    vectors = remote_embedding.embed(['海' * 3000] + ['山'] * 70)
    assert len(vectors) == 71
    assert [len(texts) for _, texts, _ in fresh] == [64, 7]
    assert len(fresh[0][1][0]) == remote_embedding.MAX_TEXT


def test_refusal_backs_off_but_a_slow_answer_does_not(monkeypatch):
    def slow(*_):
        raise TimeoutError()
    monkeypatch.setattr(remote_embedding, '_post', slow)
    assert remote_embedding.embed(['x']) is None
    assert remote_embedding.available()

    def refused(*_):
        raise OSError('HTTP 429')
    monkeypatch.setattr(remote_embedding, '_post', refused)
    assert remote_embedding.embed(['x']) is None
    assert not remote_embedding.available()


def test_query_uses_short_timeout_and_cache(fresh):
    first = remote_embedding.embed_query('还记得看海吗')
    assert remote_embedding.embed_query('还记得看海吗') == first == [1.0, 0.0]
    assert len(fresh) == 1 and fresh[0][2] == remote_embedding.QUERY_TIMEOUT


def test_prefetch_overlaps_other_work_and_the_query_waits_for_it(monkeypatch, fresh):
    import threading
    release = threading.Event()
    calls = []

    def slow_post(key, texts, timeout):
        calls.append(timeout)
        release.wait(5)
        return [[1.0, 0.0]]
    monkeypatch.setattr(remote_embedding, '_post', slow_post)
    remote_embedding.prefetch('还记得看海吗')
    remote_embedding.prefetch('还记得看海吗')  # one request per message
    threading.Timer(0.2, release.set).start()
    assert remote_embedding.embed_query('还记得看海吗') == [1.0, 0.0]
    assert calls == [remote_embedding.PREFETCH_TIMEOUT]


def test_query_gives_up_on_a_late_prefetch(monkeypatch, fresh):
    import threading
    release = threading.Event()
    monkeypatch.setattr(remote_embedding, 'QUERY_TIMEOUT', 0.1)
    monkeypatch.setattr(remote_embedding, '_post', lambda *_: release.wait(5) and [[1.0, 0.0]])
    remote_embedding.prefetch('想你了')
    assert remote_embedding.embed_query('想你了') is None
    release.set()


def test_vector_coverage(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.sqlite3')
    assert index.vector_coverage('u', 'm') == 0.0
    index.put('u', 'reply:a:1', '我们去看海', '好呀', NOW - timedelta(days=3))
    index.put('u', 'reply:b:1', '今天考试', '加油', NOW - timedelta(days=2))
    missing = index.vectors_missing('u', 'm', limit=1)
    index.put_vectors('u', 'm', [(source, digest, [1.0, 0.0]) for source, digest, _ in missing])
    assert index.vector_coverage('u', 'm') == 0.5


def _adapter(tmp_path, local_calls):
    from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter
    from tests.memory.test_mem0_memory import FakeMem0, _config

    def local_embed(text, action):
        local_calls.append(action)
        return [0.0, 0.0, 1.0] if '考试' in text else [1.0, 0.0, 0.0]
    backend = FakeMem0()
    backend.embedding_model = SimpleNamespace(embed=local_embed)
    adapter = Mem0ConversationMemoryAdapter(backend, _config(tmp_path))
    adapter._originals.put('u', 'reply:a:1', '我们去看海', '好呀', NOW - timedelta(days=3))
    adapter._originals.put('u', 'reply:b:1', '今天考试', '加油', NOW - timedelta(days=2))
    return adapter


def test_background_index_fills_cloud_and_local_vectors(tmp_path, fresh):
    local = []
    adapter = _adapter(tmp_path, local)
    assert adapter.index_original_vectors('u', limit=64) == 4
    assert adapter._originals.vector_coverage('u', remote_embedding.MODEL) == 1.0
    assert adapter._originals.vector_coverage('u', adapter.config.embedding_model) == 1.0
    assert len(fresh) == 1 and local == ['add', 'add']


def test_recall_reads_cloud_vectors_once_they_cover_the_history(tmp_path, fresh):
    local = []
    adapter = _adapter(tmp_path, local)
    adapter.index_original_vectors('u', limit=64)
    local.clear()
    hits = adapter._original_vector_hits('还记得看海吗', 'u', ())
    assert [hit.source_id for hit in hits] == ['reply:a:1']  # the other scores 0, under the floor
    assert local == []


def test_recall_falls_back_to_local_when_cloud_is_unusable_or_incomplete(tmp_path, fresh, monkeypatch):
    local = []
    adapter = _adapter(tmp_path, local)
    # Cloud vectors cover half the history: the local model answers.
    missing = adapter._originals.vectors_missing('u', remote_embedding.MODEL, limit=1)
    adapter._originals.put_vectors('u', remote_embedding.MODEL, [(s, d, [1.0, 0.0]) for s, d, _ in missing])
    hits = adapter._original_vector_hits('今天考试怎么样', 'u', ())
    assert hits and hits[0].source_id == 'reply:b:1' and 'search' in local
    assert len(fresh) == 0  # no cloud call on the reply path for indexing

    adapter.index_original_vectors('u', limit=64)
    local.clear()

    def slow(*_):
        raise TimeoutError()
    monkeypatch.setattr(remote_embedding, '_post', slow)
    hits = adapter._original_vector_hits('今天考试怎么样', 'u', ())
    assert hits and hits[0].source_id == 'reply:b:1' and local == ['search']
