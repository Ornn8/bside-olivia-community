from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from threading import Barrier

from runtime.memory.mem0_observation_time import bind_observation_time


def test_historical_dates_reach_prompt_builder_without_unsupported_add_timestamp():
    module = SimpleNamespace(generate_additive_extraction_prompt=lambda **kw: kw)
    rendezvous = Barrier(2)
    class Backend:
        def _add_to_vector_store(self, messages, metadata, filters, infer, prompt=None):
            rendezvous.wait(timeout=5)
            return module.generate_additive_extraction_prompt(new_messages=messages)
    backend = bind_observation_time(Backend(), module)
    def extract(day):
        return backend._add_to_vector_store('今天我换工作了',
            {'occurred_at':f'2026-07-{day:02d}T23:00:00+08:00'}, {}, True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(extract, [14, 15]))
    assert [r.get('timestamp') for r in results] == ['2026-07-14', '2026-07-15']
    assert all(r['new_messages'] == '今天我换工作了' for r in results)
    assert 'timestamp' not in module.generate_additive_extraction_prompt(new_messages='unrelated')


def test_context_is_reset_after_extraction_failure_and_install_is_idempotent():
    module = SimpleNamespace(generate_additive_extraction_prompt=lambda **kw: kw)
    class Backend:
        def _add_to_vector_store(self, messages, metadata, filters, infer, prompt=None):
            if messages == 'fail':
                raise RuntimeError('synthetic failure')
            return module.generate_additive_extraction_prompt(timestamp='2020-01-01')
    backend = bind_observation_time(Backend(), module)
    wrapper = backend._add_to_vector_store
    assert bind_observation_time(backend, module) is backend
    assert backend._add_to_vector_store == wrapper
    import pytest
    with pytest.raises(RuntimeError):
        backend._add_to_vector_store('fail', {'occurred_at':'2026-07-14T12:00:00+00:00'}, {}, True)
    assert 'timestamp' not in module.generate_additive_extraction_prompt()
    result = backend._add_to_vector_store('ok', {'occurred_at':'2026-07-14T12:00:00+00:00'}, {}, True)
    assert result['timestamp'] == '2020-01-01'
