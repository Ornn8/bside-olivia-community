import asyncio
import json
from types import SimpleNamespace

from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import PrivateWorldCommandService
from runtime.imports.relationship_batches import RelationshipBatches,archive_exchanges


def rows(count):
    return [{'occurred_at':None,'metadata':{'import_kind':'local_letter_backup_v1',
        'import_position':i,'user_content':f'第{i}封问候','reply_text':f'第{i}封回复'}} for i in range(count)]


class Gateway:
    def __init__(self,fail_at=None):self.calls=[];self.fail_at=fail_at
    async def complete(self,messages,**kw):
        data=json.loads(messages[1]['content']);self.calls.append(data)
        if len(self.calls)==self.fail_at:raise RuntimeError('synthetic failure')
        previous=data['previous_state'];value=previous['familiarity']+5
        return SimpleNamespace(text=json.dumps({'relationship_stage':'unknown','familiarity':value,
            'trust':value,'comfort':value,'closeness':value,'tension':0,'evidence_indexes':[1]}))


def test_sequential_batches_resume_and_deduplicate(tmp_path):
    ledger=SQLitePrivateWorldLedger(tmp_path/'world.sqlite3');service=PrivateWorldCommandService(ledger)
    queue=RelationshipBatches(tmp_path/'queue.sqlite3');exchanges=archive_exchanges(list(reversed(rows(12))))
    queue.enqueue(exchanges);gateway=Gateway(fail_at=2)
    def run(q,g):asyncio.run(q.run(gateway=g,persona_policy='policy',command_service=service,snapshot=ledger.snapshot))
    run(queue,gateway)
    assert queue.status()['processed']==5 and queue.status()['status']=='FAILED'
    assert len(gateway.calls)==2
    run(queue,gateway);assert len(gateway.calls)==2  # No automatic paid retry loop.
    queue=RelationshipBatches(tmp_path/'queue.sqlite3');queue.enqueue(exchanges);queue.retry()
    resumed=Gateway();run(queue,resumed)
    assert [len(x['ordered_exchanges']) for x in resumed.calls]==[5,2]
    assert [x['previous_state']['familiarity'] for x in resumed.calls]==[5,10]
    assert queue.status()['processed']==12 and ledger.snapshot().familiarity==15
    assert [x['user_letter'] for x in gateway.calls[0]['ordered_exchanges']]==[f'第{i}封问候' for i in range(5)]
    assert all(x['occurred_at'] is None for x in gateway.calls[0]['ordered_exchanges'])
    queue.enqueue(exchanges);run(queue,resumed);assert len(resumed.calls)==2


def test_committed_batch_recovered_without_another_llm_call(tmp_path):
    ledger=SQLitePrivateWorldLedger(tmp_path/'world.sqlite3');service=PrivateWorldCommandService(ledger)
    queue=RelationshipBatches(tmp_path/'queue.sqlite3');queue.enqueue(archive_exchanges(rows(5)))
    gateway=Gateway()
    asyncio.run(queue.run(gateway=gateway,persona_policy='policy',command_service=service,snapshot=ledger.snapshot))
    with queue.connect() as db:db.execute("UPDATE batches SET state='pending',assessment=NULL")
    reopened=RelationshipBatches(tmp_path/'queue.sqlite3')
    asyncio.run(reopened.run(gateway=gateway,persona_policy='policy',command_service=service,snapshot=ledger.snapshot))
    assert len(gateway.calls)==1 and reopened.status()['processed']==5


def test_dates_and_original_file_order(tmp_path):
    data=rows(3)
    for i,r in enumerate(data):r['occurred_at']=f'2026-09-{3-i:02}T00:00:00+00:00'
    assert [x.user_message for x in archive_exchanges(data)]==['第2封问候','第1封问候','第0封问候']
    for i,r in enumerate(data):
        r['occurred_at']=None;r['metadata'].pop('import_position')
        r['metadata']['backup_record']={'source_id':f'offline-letter-pairs:hash:{i:06}'}
    assert [x.user_message for x in archive_exchanges(list(reversed(data)))]==['第0封问候','第1封问候','第2封问候']
