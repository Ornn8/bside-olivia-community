"""Preserve canonical source speakers at vector persistence."""
from functools import wraps
from collections.abc import Mapping


def bind_history_attribution(backend):
    store = getattr(backend, 'vector_store', None)
    original = getattr(store, 'insert', None)
    if not callable(original) or getattr(original, '_olivia_history_attribution', False):
        return backend

    @wraps(original)
    def insert(vectors, payloads=None, ids=None, **kwargs):
        if payloads is None:
            return original(vectors=vectors, ids=ids, payloads=None, **kwargs)
        projected = []
        for payload in payloads:
            if (isinstance(payload, Mapping)
                    and str(payload.get('source_id', '')).startswith('history:')
                    and payload.get('domain') == 'conversation_memory'
                    and payload.get('canonical') is True
                    and payload.get('history_actor') in {'user', 'linli'}):
                payload = dict(payload, attributed_to=payload['history_actor'])
            projected.append(payload)
        return original(vectors=vectors, ids=ids, payloads=projected, **kwargs)

    insert._olivia_history_attribution = True
    store.insert = insert
    return backend
