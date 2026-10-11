"""Per-use model choice on the Olivia account: QQ chat, letters, diary.

Each use either follows the main reply model (None) or names an Olivia relay
model. Entry points mark their use with ``using``; the HTTP adapter asks
``routed_model`` for the model of each request, so billing follows the model
actually called.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import tempfile

USES = ('qq', 'letter', 'diary')
CURRENT = ContextVar('olivia_model_use', default=None)
_path = None
_cache = (None, {})


def configure(config_root):
    global _path, _cache
    _path = Path(config_root) / 'olivia_model_routes.json'
    _cache = (None, {})


def _allowed():
    from original_client_relay_api import RELAY_MODELS
    return RELAY_MODELS


def load():
    """{use: model or None}; a missing or damaged file follows the main model everywhere."""
    global _cache
    empty = {use: None for use in USES}
    if _path is None:
        return empty
    try:
        stamp = _path.stat().st_mtime_ns
    except OSError:
        return empty
    if _cache[0] == stamp:
        return dict(_cache[1])
    try:
        value = json.loads(_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return empty
    routes = {use: value.get(use) if isinstance(value, dict) and value.get(use) in _allowed() else None
              for use in USES}
    _cache = (stamp, routes)
    return dict(routes)


def validate(routes):
    if (not isinstance(routes, dict) or set(routes) != set(USES)
            or any(model is not None and model not in _allowed() for model in routes.values())):
        raise ValueError('MODEL_ROUTES_INVALID')
    return dict(routes)


def save(routes):
    routes = validate(routes)
    if _path is None:
        raise OSError('MODEL_ROUTES_UNAVAILABLE')
    _path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=_path.name + '.', dir=_path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(routes, stream)
        os.replace(temporary, _path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return routes


@contextmanager
def using(use):
    """Calls made inside (including tasks and threads started here) belong to this use."""
    token = CURRENT.set(use)
    try:
        yield
    finally:
        CURRENT.reset(token)


def routed_model(base_url, model):
    """The model for this request: the use's own choice on the Olivia account, else the main model."""
    use = CURRENT.get()
    if use is None or not model:
        return model
    from original_client_relay_api import RELAY_BASE
    if base_url.rstrip('/') != RELAY_BASE:
        return model
    return load().get(use) or model
