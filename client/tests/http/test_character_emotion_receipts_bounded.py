"""The reply's emotion step reads the receipts of its own turn, not of the whole archive."""
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]

SCRIPT = r'''
import asyncio,json,sys
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import local_server as server
import runtime.memory.received_user_originals as originals
size=int(sys.argv[1])
now=datetime(2026,9,27,2,tzinfo=timezone.utc)
old=now-timedelta(minutes=1)
first={'letter_id':'first','channel':'qq','binding_id':'test-account','source_messages':{'m1':'请慢慢来'},'content':'请慢慢来','life_received_at':old.isoformat(),'delivery_status':'FAILED','reply_text':'没有送达的草稿'}
merged={**first,'letter_id':'merged','source_messages':{'m1':'请慢慢来','m2':'不用急'},'content':'请慢慢来\n不用急','life_received_at':now.isoformat()}
chats=[{**first,'letter_id':f'chat-{i}','source_messages':{f'x{i}':f'第{i}句闲聊'},'content':f'第{i}句闲聊','delivery_status':'DELIVERED',
        'life_received_at':(now-timedelta(minutes=10+i)).isoformat()} for i in range(size*2//3)]
letters=[{'letter_id':f'letter-{i}','content':f'第{i}封信','life_received_at':(now-timedelta(days=1,minutes=i)).isoformat(),'reply_revision':1,
          'letter_status':'COMPLETED','reply_text':'回信'} for i in range(size//3)]
server.store.personal_chats[:]=[first,*chats,merged]
server.store.letters[:]=letters
class Emotion:
    def __init__(self): self.seen=[]
    async def evaluate_received(self,receipts,*,now):
        self.seen.append([(r.source_id,r.user_message,r.occurred_at.isoformat()) for r in receipts])
        return {'interpretation_only':True,'reaction_subject':'character','reactions':[]}
    def view(self,now): return {}
emotion=Emotion()
server.letters_adapter.daily_life=SimpleNamespace(emotion=emotion)
projected=[]
real=originals.received_originals
def counting(rows,**kwargs):
    rows=list(rows); projected.append(len(rows)); return real(rows,**kwargs)
originals.received_originals=counting
async def main():
    token=server._CURRENT_LETTER_MEMORY_SOURCE.set('reply:merged:1')
    try: await server.letters_adapter.prepare_character_emotion(merged['content'],now=now)
    finally: server._CURRENT_LETTER_MEMORY_SOURCE.reset(token)
asyncio.run(main())
print(json.dumps({'seen':emotion.seen,'projected':projected},ensure_ascii=True))
'''


def run(tmp_path, size):
    env = {**os.environ, 'OLIVIA_LOCAL_DATA_ROOT': str(tmp_path / str(size)), 'OLIVIA_LLM_PROVIDER': 'none',
           'OLIVIA_MEMORY_ENABLED': '0', 'PYTHONUTF8': '1', 'PYTHONPATH': str(ROOT)}
    env.pop('OLIVIA_PRIVATE_WORLD_DB', None)
    result = subprocess.run([sys.executable, '-c', SCRIPT, str(size)], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_emotion_receipts_and_projected_rows_do_not_grow_with_the_archive(tmp_path):
    small, large = run(tmp_path, 30), run(tmp_path, 3150)
    assert small['seen'] == large['seen']
    (receipts,) = large['seen']
    assert [r[1] for r in receipts] == ['请慢慢来', '不用急']
    assert receipts[0][2] == '2026-09-27T01:59:00+00:00'  # m1 keeps the receipt time of the row that first held it
    # Two projections of the turn's own rows (the merged row and the earlier row holding m1): never the archive.
    assert small['projected'] == large['projected'] == [1, 2]
