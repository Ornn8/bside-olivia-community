"""Real HTTP/canonical/world seams, with only the external provider replaced."""
import os
from pathlib import Path
import subprocess
import sys


def test_calm_conflict_then_repair_persists_and_reaches_next_letter(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = r'''
import asyncio, json, re
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
import local_server as server
from original_client_server import create_configured_original_client_server_runtime
cases = [
    ('我要求你现在停下自己的事情，按我说的做。', '我不会接受这种要求，请尊重我的安排。', 'conflict'),
    ('今天项目返工让我很烦，不是冲你。', '辛苦了，今天先缓缓。', None),
    ('对不起，我撤回干涉你的要求，尊重你的安排。', '我接受道歉，这件事我们说开了。', 'repair'),
    ('晚安。', '晚安。', None),
]
main_calls=[]
class Model:
    def timeout_seconds_for_scope(self, scope, *, default): return default
    async def complete(self, messages, **kwargs): return await self.complete_scoped(messages, **kwargs)
    async def complete_scoped(self, messages, request_id=None, **kwargs):
        if request_id and request_id.startswith('life:'):
            schema = json.loads(re.search(r'JSON (\{[^\n]+\})', messages[0]['content']).group(1))
            assert 'relationship' in schema, 'relationship classification must not be an optional afterthought'
            data = json.loads(messages[-1]['content'])
            case = next(c for c in cases if c[0] == data['user_letter'])
            assert data['linli_reply'] == case[1], (data['user_letter'], data['linli_reply'], case[1])
            signal = {'kind':case[2], 'user_quote':case[0], 'reply_quote':case[1]} if case[2] else None
            return SimpleNamespace(text=json.dumps({'updates':[], 'current_quote':None, 'relationship':signal}))
        main_calls.append(messages)
        return SimpleNamespace(text=next(c[1] for c in cases if c[0] in messages[-1]['content']))
server.letters_adapter.gateway=Model()
async def main():
    runtime=create_configured_original_client_server_runtime(server_module=server)
    states=[]
    async with TestClient(TestServer(runtime.app)) as client:
        for index, (content, _, _) in enumerate(cases):
            response=await client.post('/toy/letter/send', json={'content':content,'idempotency_key':f'continuity-{index}'})
            assert response.status==200
            letter_id=(await response.json())['data']['letter_id']
            letter=next(row for row in server.store.letters if row['letter_id']==letter_id)
            for _ in range(200):
                if letter['letter_status'] in {'COMPLETED','FAILED'}: break
                await asyncio.sleep(.01)
            assert letter['letter_status']=='COMPLETED'
            tasks=tuple(server.daily_life_tasks.values())
            if tasks: await asyncio.gather(*tasks)
            assert letter['daily_life_status']=='COMMITTED', letter.get('daily_life_error_code')
            states.append(server.private_world_port.snapshot())
        assert states[0].tension > 0
        assert states[1] == states[0], 'external frustration is not a new interpersonal conflict'
        assert states[2].tension < states[1].tension
        assert states[3] == states[2]
        followup = next(call for call in main_calls if cases[1][0] in call[-1]['content'])
        assert '"tension":"low"' in followup[0]['content']
        assert all(s.relationship_stage=='unknown' for s in states)
asyncio.run(main())
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith(('OLIVIA_', 'OPENAI_', 'DEEPSEEK_'))}
    env.update(OLIVIA_LOCAL_DATA_ROOT=str(tmp_path), OLIVIA_MEMORY_ENABLED='0',
               OLIVIA_PRIVATE_WORLD_ENABLED='true', OLIVIA_LLM_PROVIDER='openai_compatible',
               OLIVIA_LLM_BASE_URL='https://example.invalid/v1', OLIVIA_LLM_MODEL='synthetic',
               OLIVIA_LLM_REQUIRES_API_KEY='false', PYTHONUTF8='1', PYTHONPATH=str(root))
    result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
