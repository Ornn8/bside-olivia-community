"""Recall preparation gets its own budget without inflating normal generation."""
import asyncio
from types import SimpleNamespace

import pytest

from llm_gateway import Gateway, GatewayConfig, GatewayDelta, GatewayRequestScope, GatewayResponse
from runtime.memory.recall_check import RECALL_CHECK_TIMEOUT_SECONDS
from runtime.reply.reply_orchestrator import ReplyOrchestrator, ReplyRequest, ReplyState


@pytest.mark.parametrize('mode,reasoning,quality_reasoning,interpreter,previous_budget', [
    ('text_letter', False, None, False, 105),
    ('text_letter', True, 50, False, 955),
    ('text_letter', False, None, True, 115),
    ('voice_reply', False, None, False, 65),
    ('spoken_video', True, 50, False, 635),
    ('future_im', True, 50, False, 65),
])
def test_pipeline_budget_adds_one_check_and_retains_existing_quality_reserve(
    monkeypatch, mode, reasoning, quality_reasoning, interpreter, previous_budget,
):
    import local_server
    monkeypatch.setattr(local_server, 'LLM_CONFIG', GatewayConfig(timeout_seconds=30, reasoning_timeout_seconds=600))
    monkeypatch.setattr(local_server, 'LLM_TIMEOUT_SECONDS', 30)
    monkeypatch.setattr(local_server, 'supports_scoped_reasoning', lambda config: reasoning)
    monkeypatch.setattr(local_server, 'resolve_model_quality_config', lambda *a, **k: SimpleNamespace(
        timeout_seconds=10, reasoning_timeout_seconds=quality_reasoning))
    monkeypatch.setattr(local_server, 'current_turn_interpretation_enabled', lambda: interpreter)

    assert local_server._reply_pipeline_timeout_seconds(mode) == previous_budget + RECALL_CHECK_TIMEOUT_SECONDS


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('scoped', [False, True])
@pytest.mark.parametrize('prepared', [False, True])
@pytest.mark.parametrize('provider_name', ['mock', 'openai_compatible'])
def test_compatibility_bridge_budget_covers_check_but_prepared_requests_keep_generation_budget(
    monkeypatch, streaming, scoped, prepared, provider_name,
):
    from local_server import LetterAdapter, _LetterGateway
    observed_timeouts = []
    async def capture_timeout(awaitable, timeout):
        observed_timeouts.append(timeout)
        return await awaitable
    monkeypatch.setattr(asyncio, 'wait_for', capture_timeout)
    class Provider(Gateway):
        stream_enabled = streaming
        calls = 0
        def timeout_seconds_for_scope(self, scope, *, default):
            return 70 if scope is not None else default
        async def complete(self, messages, *, request_id=None):
            self.calls += 1
            return GatewayResponse('reply', request_id, 'synthetic', 'synthetic')
        async def stream(self, messages, *, request_id=None):
            self.calls += 1
            yield GatewayDelta('reply', request_id, index=0, finish_reason='stop')
    provider = Provider()
    adapter = LetterAdapter.__new__(LetterAdapter)
    adapter._runtime = (GatewayConfig(provider=provider_name, stream=streaming), provider)
    messages = ({'role': 'system', 'content': 'persona'}, {'role': 'user', 'content': 'hello'})
    adapter._messages = lambda *args: messages
    bridge = _LetterGateway(adapter)
    scope = GatewayRequestScope.MEDIA_REPLY_LOW_REASONING if scoped else None
    request = ReplyRequest(content='hello', messages=messages if prepared else None, gateway_scope=scope)

    result = asyncio.run(ReplyOrchestrator(bridge, timeout_seconds=30).run(request))

    assert result.state is ReplyState.COMPLETED
    assert provider.calls == 1
    generation_budget = 70 if scoped else 30
    expected = generation_budget + (RECALL_CHECK_TIMEOUT_SECONDS
        if not prepared and provider_name == 'openai_compatible' else 0)
    assert observed_timeouts == [expected]


def test_plain_legacy_gateway_without_timeout_resolver_keeps_default(monkeypatch):
    observed = []
    async def capture_timeout(awaitable, timeout):
        observed.append(timeout)
        return await awaitable
    monkeypatch.setattr(asyncio, 'wait_for', capture_timeout)
    class PlainGateway:
        async def complete(self, messages, *, request_id=None):
            return GatewayResponse('reply', request_id, 'synthetic', 'synthetic')

    result = asyncio.run(ReplyOrchestrator(PlainGateway(), timeout_seconds=30).run(ReplyRequest(content='hello')))

    assert result.state is ReplyState.COMPLETED
    assert observed == [30]
