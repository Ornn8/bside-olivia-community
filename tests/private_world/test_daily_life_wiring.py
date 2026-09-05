"""Fresh-process test of the final-letter → visible life → next reply seam."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("signal", [None, "support_received"])
def test_completed_letter_reaches_life_and_next_persona_prompt(tmp_path, signal):
    script = r'''
import asyncio, json, os
from datetime import datetime, timezone
from types import SimpleNamespace
import local_server as server
from runtime.reply.reply_pipeline import PipelineResult
from reply_orchestrator import ReplyState
from runtime.reply.reply_context import ReplyMode
class AcceptedReply:
    async def run(self, request, context):
        return PipelineResult('life-letter', ReplyState.COMPLETED, text='我正在慢练这首曲子的左手。', quality_status='accepted')
class LifeModel:
    async def complete(self, messages, **kwargs):
        payload = {'updates':[{'id':'piano','title':'慢练左手','detail':'我正在慢练这首曲子的左手。','status':'ongoing','kind':'linli','actor':'linli','quote':'我正在慢练这首曲子的左手。'}]}
        if os.environ.get('TEST_RELATIONSHIP_SIGNAL'):
            payload['relationship'] = {'kind':os.environ['TEST_RELATIONSHIP_SIGNAL'], 'user_quote':'你最近练琴怎么样？', 'reply_quote':'我正在慢练这首曲子的左手。'}
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))
server.reply_pipeline = AcceptedReply()
server.letters_adapter.gateway = LifeModel()
letter = {'letter_id':'life-letter','content':'你最近练琴怎么样？','reply_text':'','letter_status':'PENDING','reply_mode':ReplyMode.TEXT_LETTER.value}
server.store.letters[:] = [letter]
async def run():
    result = await server.generate_reply('life-letter', letter['content'])
    await asyncio.gather(*tuple(server.daily_life_tasks.values()))
    return result
ok = asyncio.run(run())
before_retry = server.private_world_port.snapshot()
async def retry():
    server._schedule_daily_life_exchange(letter)
    await asyncio.gather(*tuple(server.daily_life_tasks.values()))
asyncio.run(retry())
snapshot = server.daily_life_runtime.snapshot(datetime.now(timezone.utc))
fragments = server.letters_adapter.daily_life_fragments('左手练得怎么样了？')
relationship = server.private_world_port.snapshot()
print(json.dumps({'ok':ok,'status':letter.get('daily_life_status'),'relationship_status':letter.get('relationship_status'),'snapshot':snapshot,'context':[f.text for f in fragments], 'trust':relationship.trust, 'comfort':relationship.comfort, 'same_after_retry':relationship==before_retry, 'stage':relationship.relationship_stage},ensure_ascii=True))
'''
    env = {**os.environ, "OLIVIA_LOCAL_DATA_ROOT": str(tmp_path), "OLIVIA_LLM_PROVIDER": "none",
           "OLIVIA_MEMORY_ENABLED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
    env['TEST_RELATIONSHIP_SIGNAL'] = signal or ''
    env.pop("OLIVIA_PRIVATE_WORLD_DB", None)
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["status"] == "COMMITTED", payload
    assert payload["snapshot"]["projects"][0]["source_id"] == "reply:life-letter:1"
    assert "慢练" in payload["context"][0]
    assert payload['comfort'] == (1 if signal else 0)
    assert payload['same_after_retry'] is True
    assert payload['stage'] == 'unknown'
