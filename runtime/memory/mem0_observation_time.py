"""Pass canonical observation dates through the OSS extraction prompt builder."""
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
import inspect
from threading import RLock

_observation_date = ContextVar('mem0_observation_date', default=None)
_install_lock = RLock()


def bind_observation_time(backend, prompt_module=None):
    original = getattr(backend, '_add_to_vector_store', None)
    if not callable(original) or getattr(original, '_olivia_observation_time', False):
        return backend
    prompt_module = prompt_module or inspect.getmodule(original)
    builder = getattr(prompt_module, 'generate_additive_extraction_prompt', None)
    if not callable(builder):
        return backend  # Older OSS versions do not use this dated prompt.
    with _install_lock:
        builder = prompt_module.generate_additive_extraction_prompt
        if not getattr(builder, '_olivia_observation_time', False):
            @wraps(builder)
            def dated_prompt(*args, **kwargs):
                observed = _observation_date.get()
                if observed is not None and kwargs.get('timestamp') is None:
                    kwargs['timestamp'] = observed
                return builder(*args, **kwargs)
            dated_prompt._olivia_observation_time = True
            prompt_module.generate_additive_extraction_prompt = dated_prompt
    signature = inspect.signature(original)

    @wraps(original)
    def extract(*args, **kwargs):
        metadata = signature.bind(*args, **kwargs).arguments.get('metadata') or {}
        occurred_at = metadata.get('occurred_at')
        observed = datetime.fromisoformat(occurred_at).date().isoformat() if occurred_at else None
        token = _observation_date.set(observed)
        try:
            return original(*args, **kwargs)
        finally:
            _observation_date.reset(token)

    extract._olivia_observation_time = True
    backend._add_to_vector_store = extract
    return backend
