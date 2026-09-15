"""Durable, sequential five-exchange assessments, independent of Mem0 extraction."""
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import sqlite3

from runtime.imports.historical_memory import (
    HistoricalExchange, HistoricalRelationshipAssessment, assess_historical_relationship,
    apply_historical_private_world, historical_relationship_command_id,
)
from runtime.reply.reply_context import RelationshipStage


def archive_exchanges(rows):
    """Recover file order where stored; never present an invented timestamp."""
    candidates=[]
    for position,row in enumerate(rows):
        meta=row.get('metadata') or {}
        if meta.get('import_kind') not in {'local_letter_backup_v1','offline_recovered_text_reply','official_text_reply'}:
            continue
        content,reply=meta.get('user_content'),meta.get('reply_text')
        if not isinstance(content,str) or not isinstance(reply,str) or not content.strip() or not reply.strip():
            continue
        record=meta.get('backup_record') or {}
        source=record.get('source_id') or row.get('source_record_id','')
        order=meta.get('import_position',position)
        if source.startswith('offline-letter-pairs:'):
            try:order=int(source.rsplit(':',1)[1])
            except ValueError:pass
        stamp=None
        try:
            stamp=datetime.fromisoformat(str(row.get('occurred_at')).replace('Z','+00:00'))
            if stamp.tzinfo is None:stamp=None
        except ValueError:pass
        # Content identity also deduplicates the same original across import formats.
        identity=hashlib.sha256(json.dumps([content,reply],ensure_ascii=False).encode('utf-8')).hexdigest()
        candidates.append((stamp,order,position,identity,content,reply))
    candidates.sort(key=lambda x:(0,x[0],x[2]) if x[0] else (1,x[1],x[2]))
    seen=set();result=[]
    for stamp,_,__,identity,content,reply in candidates:
        if identity in seen:continue
        seen.add(identity)
        result.append(HistoricalExchange('relationship-letter:'+identity,
            stamp or datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(seconds=len(result)),
            content[:10000],reply[:20000],timestamp_known=stamp is not None))
    return tuple(result)


class RelationshipBatches:
    def __init__(self,path):
        self.path=path;path.parent.mkdir(parents=True,exist_ok=True)
        self.running=False
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, assessment TEXT, error TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS seen (source TEXT PRIMARY KEY)')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db:yield db
        finally:db.close()

    def enqueue(self,exchanges):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pending=[]
            for exchange in exchanges:
                if db.execute('SELECT 1 FROM seen WHERE source=?',(exchange.source_record_id,)).fetchone():continue
                db.execute('INSERT INTO seen VALUES (?)',(exchange.source_record_id,))
                pending.append({**asdict(exchange),'occurred_at':exchange.occurred_at.isoformat()})
            for offset in range(0,len(pending),5):
                db.execute('INSERT INTO batches(payload,state) VALUES (?,?)',(json.dumps(pending[offset:offset+5],ensure_ascii=False),'pending'))

    def retry(self):
        with self.connect() as db:db.execute("UPDATE batches SET state='pending',error=NULL WHERE state='failed'")

    def status(self):
        with self.connect() as db:
            rows=db.execute('SELECT state,payload,error FROM batches ORDER BY id').fetchall()
        total=sum(len(json.loads(p)) for _,p,_ in rows)
        completed=sum(len(json.loads(p)) for s,p,_ in rows if s=='done')
        error=next((e for s,_,e in rows if s=='failed'),None)
        return {'status':'RUNNING' if self.running else 'FAILED' if error else 'PENDING' if completed<total else 'APPLIED',
                'total':total,'processed':completed,'batches':len(rows),'batch_size':5,'error_code':error}

    async def run(self,*,gateway,persona_policy,command_service,snapshot):
        if self.running:return
        self.running=True
        try:
            while True:
                with self.connect() as db:
                    row=db.execute("SELECT id,payload,state,assessment FROM batches WHERE state!='done' ORDER BY id LIMIT 1").fetchone()
                if row is None or row[2]=='failed':return
                batch_id,payload,_,saved=row
                exchanges=tuple(HistoricalExchange(**{**x,'occurred_at':datetime.fromisoformat(x['occurred_at'])}) for x in json.loads(payload))
                try:
                    command=historical_relationship_command_id(exchanges)
                    existing=await asyncio.to_thread(command_service.lookup_command,command)
                    if existing is None:
                        if saved:
                            values=json.loads(saved);values['relationship_stage']=RelationshipStage(values['relationship_stage']);values['evidence_indexes']=tuple(values['evidence_indexes'])
                            assessment=HistoricalRelationshipAssessment(**values)
                        else:
                            current=snapshot()
                            previous={name:getattr(current,name) for name in ('familiarity','trust','comfort','closeness','tension')}
                            assessment=await assess_historical_relationship(exchanges,gateway=gateway,persona_policy=persona_policy,previous_state=previous,preserve_order=True)
                            with self.connect() as db:db.execute('UPDATE batches SET assessment=? WHERE id=?',(json.dumps(asdict(assessment)),batch_id))
                        await asyncio.to_thread(apply_historical_private_world,exchanges,assessment=assessment,command_service=command_service,preserve_order=True)
                    with self.connect() as db:db.execute("UPDATE batches SET state='done',error=NULL WHERE id=?",(batch_id,))
                except asyncio.CancelledError:raise
                except Exception as exc:
                    code=getattr(exc,'code','HISTORY_RELATIONSHIP_FAILED')
                    if not isinstance(code,str) or not code.replace('_','').isalnum():code='HISTORY_RELATIONSHIP_FAILED'
                    with self.connect() as db:db.execute("UPDATE batches SET state='failed',error=? WHERE id=?",(code,batch_id))
                    return
        finally:self.running=False
