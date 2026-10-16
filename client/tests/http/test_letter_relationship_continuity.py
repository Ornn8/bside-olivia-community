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
from runtime.reply import jev_questions
class WorldSelector:
    async def ask(self, state, questions, *, purpose):
        if purpose == 'reply-world-selection':
            return {key: 'must' for key in questions}
        if purpose == 'exchange-world-update':
            return {'world_update': 'none'}  # These exchanges change no physical activity.
        assert purpose in {'exchange-facts', 'exchange-conduct'}, purpose
        answers = {key: 'none' if 'none' in q['criteria'] else next(iter(q['criteria']))
                   for key, q in questions.items()}
        if purpose == 'exchange-conduct':
            assert 'linli_reply' not in state['sources']
            assert state['sources']['user_letter'] == cases[0][0]
            return {**answers, 'conduct':'pressure', 'quote':'u0'}
        case = next(c for c in cases if c[0] == state['sources']['user_letter'])
        assert state['sources']['linli_reply'] == case[1]
        exchange_calls.append(case[0])
        if case[2]:
            answers.update(relationship=case[2], relationship_user_quote='u0', relationship_reply_quote='r0')
        return answers
jev_questions.configured_questions = lambda: WorldSelector()
cases = [
    ('我要求你现在停下自己的事情，按我说的做。', '我不会接受这种要求，请尊重我的安排。', 'conflict'),
    ('今天项目返工让我很烦，不是冲你。', '辛苦了，今天先缓缓。', None),
    ('对不起，我撤回干涉你的要求，尊重你的安排。', '我接受道歉，这件事我们说开了。', 'repair'),
    ('晚安。', '晚安。', None),
]
main_calls=[]
exchange_calls=[]
class Model:
    def timeout_seconds_for_scope(self, scope, *, default): return default
    async def complete(self, messages, **kwargs): return await self.complete_scoped(messages, **kwargs)
    async def complete_scoped(self, messages, request_id=None, **kwargs):
        assert not (request_id or '').startswith('life:'), 'JEV owns exchange semantics'
        main_calls.append(messages)
        return SimpleNamespace(text=next(c[1] for c in cases if c[0] in messages[-1]['content']))
server.letters_adapter.gateway=Model()
server._current_life_rhythm=lambda: {}
async def main():
    from datetime import datetime, timedelta, timezone
    from runtime.relationship_batch import BatchQueue, tick
    async def judge(gateway, batch, chosen):
        rows=[]
        for turn in batch['turns']:
            case=next(c for c in cases if c[0]==turn['user'])
            friction=case[2] or 'none'
            rows.append({'id':turn['id'],'signals':{'rel_friction':friction},
                'moment':'rel_friction:'+friction if friction!='none' else None,
                'user_quote':turn['user'],'reply_quote':turn['reply']})
        return {'turns':rows}
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
            assert letter['letter_status']=='COMPLETED', (letter.get('error_code'), letter.get('reply_error_code'))
            tasks=tuple(server.daily_life_tasks.values())
            if tasks: await asyncio.gather(*tasks)
            assert letter['daily_life_status']=='COMMITTED', letter.get('daily_life_error_code')
            # Simulate the daily background settlement independently of reply delivery.
            now=datetime.now(timezone.utc)
            queue=BatchQueue(server.private_world_relationship_committer.ledger._database_path)
            queue.sync(server.store.letters,now-timedelta(days=2))
            # The regular store save may already have queued the message at delivery.
            with queue.db() as db:
                db.execute('UPDATE relationship_queue SET queued_at=?',(now-timedelta(days=2),))
            assert await tick(server,now,judge)=='updated'
            states.append(server.private_world_port.snapshot())
        assert states[0].tension > 0
        assert exchange_calls == [case[0] for case in cases]
        assert states[1].tension == states[0].tension, 'external frustration is not a new interpersonal conflict'
        assert states[2].tension < states[1].tension
        assert states[3].tension == states[2].tension
        followup = next(call for call in main_calls if cases[1][0] in call[-1]['content'])
        projected = json.loads(re.search(r'<private_behavior>\s*(.*?)\s*</private_behavior>', followup[0]['content'], re.S)[1])
        assert '当前交流较平和' in projected['expression_context']
        assert 'tension' not in projected
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
