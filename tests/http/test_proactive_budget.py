import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('base_url,model,expected', [
    ('https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen3.8-max', {'enable_thinking': False}),
    ('https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen3.8-flash', {'enable_thinking': False}),
    ('https://api.deepseek.com', 'deepseek-v4-flash', {'thinking': {'type': 'disabled'}}),
    ('https://example.invalid/v1', 'custom', {}),
])
def test_planning_keeps_model_without_forcing_reasoning(base_url, model, expected):
    from llm_gateway import GatewayConfig, GatewayRequestScope, OpenAICompatibleAdapter
    gateway = OpenAICompatibleAdapter(GatewayConfig(
        provider='openai_compatible', base_url=base_url, model=model))
    scope = GatewayRequestScope.PROACTIVE_PLANNING
    assert not gateway._uses_max_reasoning(scope)
    body = gateway._body([{'role': 'user', 'content': 'Decide.'}], stream=False, scope=scope)
    assert body['model'] == model
    for key, value in expected.items():
        assert body[key] == value
    assert 'reasoning_effort' not in body
    assert 'max_completion_tokens' not in body
    if not expected:
        assert 'enable_thinking' not in body and 'thinking' not in body


@pytest.mark.parametrize('outcome', ['defer', 'invalid', 'failure', 'send'])
def test_paid_planning_is_bounded_across_ticks_and_restarts(tmp_path, outcome):
    script = r'''
import asyncio, json, os
from types import SimpleNamespace
import local_server as server
from llm_gateway import GatewayRequestScope
from runtime.reply.proactive_letters import write_json
root = server._state_root()
now = [1000000.0]
server.time.time = lambda: now[0]
server._proactive_ready = lambda: True
server.store.letters[:] = [{'letter_id':'synthetic', 'content':'past event',
    'reply_text':'reply', 'letter_status':'COMPLETED', 'created_at':now[0]-4000}]
write_json(root / 'proactive/settings.json', {'enabled':True})
calls, published = [], []
real_complete = server._proactive_complete
assembled, requests = [], []
def messages(query):
    assembled.append(query)
    return [{'role':'system', 'content':'synthetic-persona-marker'}]
async def gateway_complete(messages, **kwargs):
    requests.append((messages, kwargs))
    return SimpleNamespace(text='synthetic response')
server.letters_adapter._messages = messages
server.letters_adapter.gateway = SimpleNamespace(
    complete_scoped=gateway_complete, timeout_seconds_for_scope=lambda *a, **kw: 10)
async def complete(intent, **kwargs):
    calls.append(intent['id'])
    if os.environ['OUTCOME'] == 'failure':
        raise ValueError('synthetic failure')
    if os.environ['OUTCOME'] == 'invalid':
        return '{}'
    return json.dumps({'decision':os.environ['OUTCOME'], 'format':'text', 'title':'later'})
async def publish(intent, plan):
    published.append(intent['id'])
server._proactive_complete = complete
server._publish_proactive = publish
async def tick():
    try:
        await server._proactive_tick()
    except ValueError:
        pass
async def main():
    intent = {'id':'synthetic', 'source_id':'reply:synthetic:1'}
    await real_complete(intent, planning=True)
    assert not assembled
    assert requests[-1][1]['scope'] is GatewayRequestScope.PROACTIVE_PLANNING
    assert 'synthetic-persona-marker' not in requests[-1][0][0]['content']
    await real_complete(intent, planning=False)
    assert assembled == ['past event']
    assert requests[-1][1]['scope'] is GatewayRequestScope.BACKGROUND_REASONING
    assert 'synthetic-persona-marker' in requests[-1][0][0]['content']
    server._refresh_proactive_context()
    for _ in range(144):
        # Reset transient state as on restart; only disk state may limit calls.
        server._proactive_reason = 'waiting'
        await tick()
        now[0] += 300
    assert len(calls) == 1, len(calls)
    assert len(published) == (1 if os.environ['OUTCOME'] == 'send' else 0)
    for revision in range(2, 7):
        server.store.letters[0]['reply_revision'] = revision
        server._refresh_proactive_context()
        await tick()
        now[0] += 3600
    assert len(calls) == 3, len(calls)
    now[0] += 86400
    server._refresh_proactive_context()
    await tick()
    assert len(calls) == 4
asyncio.run(main())
'''
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('OLIVIA_', 'OPENAI_', 'DEEPSEEK_'))}
    env.update(OLIVIA_LOCAL_DATA_ROOT=str(tmp_path), OLIVIA_MEMORY_ENABLED='0',
               OLIVIA_PRIVATE_WORLD_ENABLED='false', OLIVIA_LLM_PROVIDER='openai_compatible',
               OLIVIA_LLM_BASE_URL='https://example.invalid/v1', OLIVIA_LLM_MODEL='synthetic',
               OLIVIA_LLM_REQUIRES_API_KEY='false', PYTHONUTF8='1',
               PYTHONPATH=str(Path(__file__).resolve().parents[2]), OUTCOME=outcome)
    result = subprocess.run([sys.executable, '-c', script], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
