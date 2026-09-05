"""Fresh-process test of the final-letter → visible life → next reply seam."""
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_completed_letter_reaches_life_and_next_persona_prompt(tmp_path):
    script = r'''
import asyncio, json
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
        return SimpleNamespace(text=json.dumps({'updates':[{'id':'piano','title':'慢练左手','detail':'我正在慢练这首曲子的左手。','status':'ongoing','kind':'linli','actor':'linli','quote':'我正在慢练这首曲子的左手。'}]}, ensure_ascii=False))
server.reply_pipeline = AcceptedReply()
server.letters_adapter.gateway = LifeModel()
letter = {'letter_id':'life-letter','content':'你最近练琴怎么样？','reply_text':'','letter_status':'PENDING','reply_mode':ReplyMode.TEXT_LETTER.value}
server.store.letters[:] = [letter]
async def run():
    result = await server.generate_reply('life-letter', letter['content'])
    await asyncio.gather(*tuple(server.daily_life_tasks.values()))
    return result
ok = asyncio.run(run())
snapshot = server.daily_life_runtime.snapshot(datetime.now(timezone.utc))
fragments = server.letters_adapter.daily_life_fragments('左手练得怎么样了？')
print(json.dumps({'ok':ok,'status':letter.get('daily_life_status'),'snapshot':snapshot,'context':[f.text for f in fragments]},ensure_ascii=True))
'''
    env = {**os.environ, "OLIVIA_LOCAL_DATA_ROOT": str(tmp_path), "OLIVIA_LLM_PROVIDER": "none",
           "OLIVIA_MEMORY_ENABLED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
    env.pop("OLIVIA_PRIVATE_WORLD_DB", None)
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["status"] == "COMMITTED"
    assert payload["snapshot"]["projects"][0]["source_id"] == "reply:life-letter:1"
    assert "慢练" in payload["context"][0]
