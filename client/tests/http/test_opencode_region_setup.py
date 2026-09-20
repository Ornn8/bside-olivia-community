import asyncio
import json
import pytest
import original_client_setup_api as setup


@pytest.mark.parametrize('error_type,expected', [
    ('RegionError', 'LLM_SETUP_REGION_OPT_IN_REQUIRED'),
    ('AuthenticationError', 'LLM_SETUP_CONNECTION_FAILED')])
def test_region_opt_in_is_safe_and_specific(monkeypatch, error_type, expected):
    class Response:
        status = 403
        content = None
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def read(self, limit):
            return json.dumps({'error': {'type': error_type, 'message': 'private provider body'}}).encode()
    response = Response()
    response.content = response
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, *args, **kwargs): return response
    monkeypatch.setattr(setup, 'ClientSession', Session)
    with pytest.raises(setup.LLMSetupError) as error:
        asyncio.run(setup._probe_openai_compatible('https://opencode.ai/zen/go/v1', 'deepseek-v4-flash', 'synthetic-key'))
    assert str(error.value) == expected
