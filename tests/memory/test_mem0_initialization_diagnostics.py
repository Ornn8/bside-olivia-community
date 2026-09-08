import pytest
from runtime.memory.mem0_memory import _initialization_step, Mem0AdapterError


@pytest.mark.parametrize('module,component', [
    ('qdrant_client.local', 'VECTOR_STORE'), ('transformers.modeling', 'EMBEDDING'),
    ('openai.client', 'LLM_CLIENT'), ('sqlite3', 'SQLITE'), ('mem0.main', 'MEM0'),
    ('private_package_name', 'APP'),
])
def test_backend_failure_has_only_fixed_stage_component_and_type(module, component):
    namespace = {'__name__': module}
    exec(compile('def fail():\n    raise ValueError("private key URL and path")', 'private-filename', 'exec'), namespace)
    with pytest.raises(Mem0AdapterError) as failure:
        _initialization_step('BACKEND', namespace['fail'])
    assert str(failure.value) == f'MEM0_INIT_BACKEND_{component}_VALUE'
    assert 'private' not in str(failure.value)


def test_outer_factory_preserves_inner_diagnostic():
    def backend():
        raise TypeError('private data')
    with pytest.raises(Mem0AdapterError, match='^MEM0_INIT_BACKEND_APP_TYPE$'):
        _initialization_step('FACTORY', _initialization_step, 'BACKEND', backend)
