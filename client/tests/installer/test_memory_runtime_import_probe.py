from types import SimpleNamespace

import pytest

from installer import verify_mem0_runtime as verifier


@pytest.mark.parametrize('failure', ['import', 'storage', None])
def test_runtime_verification_imports_and_opens_only_ephemeral_storage(monkeypatch, failure):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {'location': ':memory:'}
            calls.append('open')

        def get_collections(self):
            if failure == 'storage':
                raise ImportError('private path')
            calls.append('read')

        def close(self):
            calls.append('close')

    def load(name):
        assert name in {'mem0', 'qdrant_client'}
        calls.append(name)
        if failure == 'import':
            raise ImportError('private dependency details')
        return SimpleNamespace(QdrantClient=Client)

    monkeypatch.setattr(verifier.importlib, 'import_module', load)
    assert verifier.verify_vector_runtime() is (failure is None)
    if failure != 'import':
        assert calls[-1] == 'close'
