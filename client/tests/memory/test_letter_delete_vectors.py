from types import SimpleNamespace
from uuid import UUID

import pytest

from runtime.imports.letter_delete import Sources
from runtime.imports.letter_delete_runtime import _points, _apply_points


def test_real_local_vectors_paginate_keep_other_users_and_restore(tmp_path):
    qdrant = pytest.importorskip('qdrant_client')
    from qdrant_client import models
    client = qdrant.QdrantClient(path=str(tmp_path / 'vectors'))
    try:
        client.create_collection('memory', vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
        records = [models.PointStruct(id=str(UUID(int=i + 1)), vector=[1.0, 0.0], payload={
            'user_id': 'local-user', 'agent_id': 'test', 'source_id': 'reply:a:1' if i in {0, 269} else 'reply:b:1',
            'data': 'synthetic memory ' + str(i)}) for i in range(270)]
        shared = records[268].model_copy(deep=True)
        shared.payload.update(source_id='reply:a:1', source_ids=['reply:a:1', 'reply:b:1'])
        records[268] = shared
        records.append(models.PointStruct(id=str(UUID(int=999)), vector=[0.0, 1.0],
            payload={'user_id': 'other', 'agent_id': 'test', 'source_id': 'reply:a:1', 'data': 'other user'}))
        client.upsert('memory', points=records)
        store = SimpleNamespace(client=client, collection_name='memory',
            _create_filter=lambda values: models.Filter(must=[models.FieldCondition(key=k, match=models.MatchValue(value=v)) for k, v in values.items()]))
        adapter = SimpleNamespace(backend=SimpleNamespace(vector_store=store), _evidence_cache={'cached': 'old'},
            _configured_user_aliases=lambda user: (user,), _provider_filters=lambda user: {'user_id': user, 'agent_id': 'test'})
        plan = _points(adapter, Sources({'letter_id': 'a'}, 'user'))
        assert len(plan) == 3
        _apply_points(adapter, plan)
        _apply_points(adapter, plan)
        assert client.count('memory').count == 269
        assert client.retrieve('memory', ids=[str(UUID(int=999))])[0].payload['data'] == 'other user'
        assert client.retrieve('memory', ids=[str(UUID(int=269))])[0].payload['source_ids'] == ['reply:b:1']
        assert client.retrieve('memory', ids=[str(UUID(int=269))])[0].payload['source_id'] == 'reply:b:1'
        _apply_points(adapter, plan, restore=True)
        assert client.count('memory').count == 271
        restored = client.retrieve('memory', ids=[str(UUID(int=269))], with_vectors=True)[0]
        assert restored.payload == shared.payload
        assert restored.vector == shared.vector
        assert adapter._evidence_cache == {}
    finally:
        client.close()
