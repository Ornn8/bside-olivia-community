"""Cloud text embeddings for memory recall, through the Olivia relay.

The relay's model ranks earlier exchanges by meaning far better than the local
bge-small (recall@5 0.92 vs 0.82 on the frozen recall set), costs the user
nothing, and needs the Olivia account key. Every failure returns None so recall
falls back to the local model; nothing here may block or fail a reply.
"""
import json
import os
import threading
import time
import urllib.request

MODEL = 'olivia-embed-1'
# Cosine scores from this model sit higher and closer together than bge's: on
# the recall set no correct top-12 hit scored below 0.65.
SCORE_FLOOR = 0.6
MAX_BATCH = 64
MAX_TEXT = 2000
QUERY_TIMEOUT = float(os.environ.get('OLIVIA_REMOTE_EMBEDDING_QUERY_TIMEOUT', '4'))
INDEX_TIMEOUT = 30.0
PREFETCH_TIMEOUT = 15.0
_COOLDOWN = 300.0

_get_key = None
_lock = threading.Lock()
_queries: dict[str, list[float]] = {}
_pending: dict[str, threading.Event] = {}
_failed_at = 0.0


def configure(get_key):
    global _get_key
    _get_key = get_key


def _key():
    if os.environ.get('OLIVIA_REMOTE_EMBEDDING', '1') == '0' or not callable(_get_key):
        return None
    try:
        key = _get_key()
    except Exception:
        return None
    key = key.strip() if isinstance(key, str) else ''
    return key if key.startswith('olivia-') and len(key) > len('olivia-') else None


def available():
    return _key() is not None and time.monotonic() - _failed_at >= _COOLDOWN


def _post(key, texts, timeout):
    from runtime.official_endpoints import API_BASE
    from runtime.tls import client_tls_context
    from runtime.reply.companion_decision import _NoRedirect
    body = json.dumps({'model': MODEL, 'input': texts}, ensure_ascii=False).encode()
    request = urllib.request.Request(API_BASE + '/embeddings', data=body, method='POST',
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                        urllib.request.HTTPSHandler(context=client_tls_context()))
    with opener.open(request, timeout=timeout) as response:
        rows = json.loads(response.read(16 * 1024 * 1024))['data']
    rows = sorted(rows, key=lambda row: row['index'])
    if [row['index'] for row in rows] != list(range(len(texts))):
        raise ValueError('incomplete embedding response')
    vectors = []
    for row in rows:
        vector = [float(value) for value in row['embedding']]
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        vectors.append([value / norm for value in vector])
    return vectors


def embed(texts, *, timeout=INDEX_TIMEOUT):
    """Unit vectors for texts (each cut to MAX_TEXT), or None when the cloud is unusable."""
    global _failed_at
    key = _key()
    if key is None or not texts or time.monotonic() - _failed_at < _COOLDOWN:
        return None
    texts = [text[:MAX_TEXT] for text in texts]
    try:
        vectors = []
        for start in range(0, len(texts), MAX_BATCH):
            vectors += _post(key, texts[start:start + MAX_BATCH], timeout)
        return vectors
    except Exception as exc:
        # A slow answer only skips this read. A refusal or an outage backs off,
        # so it costs one failed call, not one per reply.
        if not isinstance(exc, TimeoutError) and not isinstance(getattr(exc, 'reason', None), TimeoutError):
            _failed_at = time.monotonic()
        return None


def _remember(text, vector):
    with _lock:
        if len(_queries) >= 256:
            _queries.clear()
        _queries[text] = vector


def prefetch(text):
    """Start embedding a reply's message in the background while other stages run."""
    if not isinstance(text, str) or not text.strip() or not available():
        return
    text = text[:500]
    with _lock:
        if text in _queries or text in _pending:
            return
        done = _pending[text] = threading.Event()

    def run():
        try:
            result = embed([text], timeout=PREFETCH_TIMEOUT)
            if result:
                _remember(text, result[0])
        finally:
            with _lock:
                _pending.pop(text, None)
            done.set()
    threading.Thread(target=run, name='olivia-embed-prefetch', daemon=True).start()


def embed_query(text):
    """The query's vector within QUERY_TIMEOUT, or None; prefetched and repeated queries hit a cache."""
    text = text[:500]
    with _lock:
        cached, pending = _queries.get(text), _pending.get(text)
    if cached is not None:
        return cached
    if pending is not None:
        pending.wait(QUERY_TIMEOUT)
        with _lock:
            return _queries.get(text)
    result = embed([text], timeout=QUERY_TIMEOUT)
    if not result:
        return None
    _remember(text, result[0])
    return result[0]
