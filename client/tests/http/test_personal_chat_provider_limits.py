import asyncio
import json
from types import SimpleNamespace
import uuid

import pytest

from llm_gateway import GatewayError, _check_provider_quota
from runtime.diagnostics.support_bundle import project_chat_task
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


@pytest.mark.parametrize('wire_code,status,expected', [
    ('usage_reconciliation_required', 409, 'PROVIDER_USAGE_PENDING'),
    ('pending_requests_limit', 429, 'PROVIDER_BUSY'),
])
def test_relay_account_block_preserves_terminal_meaning(wire_code, status, expected):
    async def body():
        return json.dumps({'error': {'code': wire_code, 'message': 'private text'}})
    response = SimpleNamespace(status=status, text=body, headers={})
    with pytest.raises(GatewayError) as caught:
        asyncio.run(_check_provider_quota(response))
    assert caught.value.code == expected
    assert caught.value.retryable is False
    assert caught.value.diagnostic_stage == 'http_response'


def test_recovered_turn_keeps_both_safe_failure_receipts():
    traces = [str(uuid.uuid4()), str(uuid.uuid4())]
    rows = []
    persisted = []
    event = PersonalMessage('qq', 'synthetic-bot', 'synthetic-owner', '1', 'synthetic input')
    async def generate(event, row):
        exc = RuntimeError('PERSONAL_CHAT_PROVIDER_RETRYABLE')
        exc.retryable = True
        exc.failure_context = {'provider_code': 'PROVIDER_RETRYABLE', 'http_status': 503,
            'failure_stage': 'http_response', 'provider_request_id': traces[row['generation_attempts'] - 1],
            'message': 'private error', 'api_key': 'private key'}
        raise exc
    async def unused(*args):
        raise AssertionError('failed generation cannot send or commit')
    service = PersonalChatService(rows, lambda: persisted.append(json.loads(json.dumps(rows))),
                                  generate, unused, {'qq': ('synthetic-bot', 'synthetic-owner')})
    async def run():
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await service.handle(event, unused)
    asyncio.run(run())
    receipts = project_chat_task(persisted[-1][0]).get('generation_failures', [])
    assert [item['provider_request_id'] for item in receipts] == traces
    assert [item['generation_attempt'] for item in receipts] == [1, 2]
    assert all(item['retryable'] is True for item in receipts)
    assert 'private' not in json.dumps(receipts)
    assert project_chat_task(project_chat_task(rows[0])) == project_chat_task(rows[0])


def test_provider_receipts_reject_unrelated_shaped_private_metadata():
    context = {'provider_code': 'PROVIDER_RETRYABLE', 'http_status': 503,
        'cause_code': 'LLM_SYNTHETIC_PRIVATE_TOKEN_123456789',
        'route_missing_fields': ['mode'], 'route_extra_field_count': 42}
    projected = project_chat_task({'channel': 'qq', 'generation_failure_context': context,
        'generation_failures': [{**context, 'generation_attempt': 1, 'retryable': True}]})
    assert projected['generation_failure_context'] == {'provider_code': 'PROVIDER_RETRYABLE', 'http_status': 503}
    assert projected['generation_failures'] == [{'provider_code': 'PROVIDER_RETRYABLE', 'http_status': 503,
                                                'generation_attempt': 1, 'retryable': True}]
