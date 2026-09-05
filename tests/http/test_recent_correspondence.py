"""Real send/persist/next-send boundary with only the external LLM replaced."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_older_recall_does_not_reinforce_historical_assistant_inventions():
    from runtime.reply.recent_correspondence import recent_correspondence
    rows = [{"letter_id": str(index), "reply_revision": 1, "letter_status": "COMPLETED",
             "private_world_occurred_at": f"2026-09-05T01:0{index}:00+00:00",
             "content": "那次剧院演出只是我编造的场景。" if index == 0 else f"今天读第{index}本书。",
             "reply_text": "你已经问了七遍，答案始终一样。" if index == 0 else "慢慢读。"}
            for index in range(5)]
    context = recent_correspondence(rows, query="那次剧院演出发生过吗？")
    assert "只是我编造的场景" in context
    assert "七遍" not in context
    assert "慢慢读" in context  # Immediate back-and-forth is still available.


@pytest.mark.parametrize('gap', [0, 4])
def test_next_http_letter_keeps_recent_canonical_context_without_mem0(tmp_path, gap):
    script = r'''
import asyncio, json, os
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
import local_server as server
from original_client_server import create_configured_original_client_server_runtime
calls=[]
class ExternalModel:
    def timeout_seconds_for_scope(self, scope, *, default):
        return default
    async def complete(self, messages, **kwargs):
        return await self.complete_scoped(messages, **kwargs)
    async def complete_scoped(self, messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(text='收到，这只是你编的场景。')
server.letters_adapter.gateway=ExternalModel()
async def main():
    runtime=create_configured_original_client_server_runtime(server_module=server)
    async with TestClient(TestServer(runtime.app)) as client:
        contents = ['我们去年在大阪看演出只是我编的场景，不是真的。']
        contents += [f'今晚我在收拾桌子，换了第{i}本书。' for i in range(int(os.environ['TEST_GAP']))]
        contents += ['我们去年在大阪看的那个演出是真的发生过吗？']
        for content in contents:
            response=await client.post('/toy/letter/send', json={'content':content})
            assert response.status==200, await response.text()
            letter=next(row for row in server.store.letters if row['content']==content)
            for _ in range(100):
                if letter['letter_status'] in {'COMPLETED','FAILED'}: break
                await asyncio.sleep(.01)
            assert letter['letter_status']=='COMPLETED', letter.get('error_code')
    print(json.dumps({'calls':calls,'letters':len(server.store.letters)},ensure_ascii=True))
asyncio.run(main())
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith(('OLIVIA_', 'OPENAI_', 'DEEPSEEK_'))}
    env.update(OLIVIA_LOCAL_DATA_ROOT=str(tmp_path), OLIVIA_MEMORY_ENABLED='0',
               OLIVIA_PRIVATE_WORLD_ENABLED='false', OLIVIA_LLM_PROVIDER='openai_compatible',
               OLIVIA_LLM_BASE_URL='https://example.invalid/v1', OLIVIA_LLM_MODEL='synthetic',
               OLIVIA_LLM_REQUIRES_API_KEY='false', PYTHONUTF8='1', PYTHONPATH=str(ROOT))
    env['TEST_GAP'] = str(gap)
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload['letters'] == 2 + gap
    assert len(payload['calls']) == 2 + gap
    context = json.dumps(payload['calls'][-1], ensure_ascii=False)
    assert '只是我编的场景，不是真的' in context
    assert '收到，这只是你编的场景' in context
