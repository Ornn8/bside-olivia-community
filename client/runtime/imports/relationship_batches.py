"""Durable, sequential five-exchange assessments, independent of Mem0 extraction."""
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
import sqlite3

from runtime.diagnostics.history_relationship import project_history_failure_context
from runtime.imports.historical_memory import (
    HistoricalExchange, HistoricalRelationshipAssessment, assess_historical_relationship,
    apply_historical_private_world, exchange_days, historical_relationship_command_id,
)
from runtime.reply.reply_context import RelationshipStage


# Refusals that pass: the relay or JEV is busy (429), a server error, or no answer in time. A batch that meets one
# is tried again in place after growing pauses and only then marked failed; a batch failed this way is taken up again
# the next time the re-read starts (startup, an import, a retry). Any other failure stays failed until a retry.
TRANSIENT_ERRORS = frozenset('PRIVATE_WORLD_HISTORY_' + code for code in (
    *(f'JEV_HTTP_{status}' for status in (429, 502, 503, 504)),
    *(f'JEV_PROVIDER_HTTP_{status}' for status in (429, 500, 502, 503, 504, 529)),
    *(f'JEV_BILLING_HTTP_{status}' for status in (429, 502, 503, 504)),
    'JEV_TIMEOUT', 'JEV_UNAVAILABLE', 'JEV_PROVIDER_CONNECT_FAILED', 'JEV_PROVIDER_READ_FAILED',
    'JEV_BILLING_UNAVAILABLE', 'LLM_RATE_LIMITED', 'LLM_TIMEOUT', 'LLM_UNAVAILABLE', 'LLM_RETRYABLE'))
RETRY_DELAYS = (15, 40, 120, 300)  # seconds before the 2nd to 5th try of one batch
# The re-read waits while a reply to him is being written: one account has few requests in flight at the relay.
LIVE_POLL_SECONDS = 2
LIVE_WAIT_SECONDS = 300  # a reply that never reports done cannot hold the re-read back for good
HISTORY_DAYS_EVENT = 'relationship_history_days'
_HISTORY_DAYS_KEY = 'history_days_merged'
_HISTORY_DAYS_PER_EVENT = 400  # an event payload holds at most 8 KiB


def transient_failure(code, exc=None):
    if code in TRANSIENT_ERRORS:
        return True
    # A status the code does not name: a plain server error from the JEV endpoint.
    context = getattr(getattr(exc, '__cause__', None), 'failure_context', None)
    status = context.get('http_status') if isinstance(context, dict) else None
    return code == 'PRIVATE_WORLD_HISTORY_JEV_HTTP_ERROR' and type(status) is int and 500 <= status <= 599


def _exchanges(payload):
    return tuple(HistoricalExchange(**{**x,'occurred_at':datetime.fromisoformat(x['occurred_at'])}) for x in json.loads(payload))


def _busy(source):
    try:
        return bool(source())
    except Exception:
        return False  # a broken source must never stop the re-read


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
    retry_delays = RETRY_DELAYS
    live_poll_seconds = LIVE_POLL_SECONDS
    live_wait_seconds = LIVE_WAIT_SECONDS

    def __init__(self,path):
        self.path=path;path.parent.mkdir(parents=True,exist_ok=True)
        self.running=False
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, assessment TEXT, error TEXT, failure_context TEXT)')
            if 'failure_context' not in {row[1] for row in db.execute('PRAGMA table_info(batches)')}:
                db.execute('ALTER TABLE batches ADD COLUMN failure_context TEXT')
            db.execute('CREATE TABLE IF NOT EXISTS seen (source TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

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
        with self.connect() as db:db.execute("UPDATE batches SET state='pending',error=NULL,failure_context=NULL WHERE state='failed'")

    def retry_transient(self):
        """Take up again the batches that failed on a refusal that passes; others stay failed."""
        with self.connect() as db:
            ids=[]
            for batch_id,error,context in db.execute("SELECT id,error,failure_context FROM batches WHERE state='failed'").fetchall():
                try:
                    marked=bool(context) and json.loads(context).get('transient') is True
                except (ValueError,TypeError,AttributeError):
                    marked=False
                if error in TRANSIENT_ERRORS or marked:
                    ids.append((batch_id,))
            db.executemany("UPDATE batches SET state='pending',error=NULL,failure_context=NULL WHERE id=?",ids)
        return len(ids)

    def status(self):
        with self.connect() as db:
            rows=db.execute('SELECT state,payload,error,failure_context FROM batches ORDER BY id').fetchall()
        total=sum(len(json.loads(p)) for _,p,_,_ in rows)
        completed=sum(len(json.loads(p)) for s,p,_,_ in rows if s=='done')
        failed=next((row for row in rows if row[0]=='failed'),None)
        error=failed[2] if failed else None
        result = {'status':'RUNNING' if self.running else 'FAILED' if failed else 'PENDING' if completed<total else 'APPLIED',
                'total':total,'processed':completed,'batches':len(rows),'batch_size':5,'error_code':error}
        if failed:
            try:
                context=json.loads(failed[3]) if failed[3] else {}
            except (ValueError, TypeError):
                context={}
            result['failure_context']=project_history_failure_context(context)
        return result

    async def _yield_to_live(self,busy):
        """Wait while a reply to him is being written, so the re-read never takes its place at the relay."""
        if busy is None:return
        waited=0
        while waited<self.live_wait_seconds and _busy(busy):
            await asyncio.sleep(self.live_poll_seconds);waited+=self.live_poll_seconds

    async def run(self,*,gateway,persona_policy,command_service,snapshot,busy=None):
        """Assess the waiting batches one at a time, oldest first.

        A refusal that passes (TRANSIENT_ERRORS) is tried again after each pause of retry_delays; the batch is
        marked failed after the last one, or at once for any other failure, and the run stops there. `busy`
        (optional) is true while a live reply is being written; each model call waits for it first."""
        if self.running:return
        self.running=True
        try:
            # Reuse stored assessments from earlier versions; no model calls.
            with self.connect() as db:
                completed = db.execute("SELECT payload,assessment FROM batches WHERE state='done' AND assessment IS NOT NULL ORDER BY id").fetchall()
            for payload, saved in completed:
                exchanges=_exchanges(payload)
                command=historical_relationship_command_id(exchanges) + '.stage-v2'
                if await asyncio.to_thread(command_service.lookup_command, command) is not None:
                    continue
                values=json.loads(saved);values['relationship_stage']=RelationshipStage(values['relationship_stage']);values['evidence_indexes']=tuple(values['evidence_indexes'])
                await asyncio.to_thread(apply_historical_private_world, exchanges,
                    assessment=HistoricalRelationshipAssessment(**values), command_service=command_service,
                    preserve_order=True, stage_backfill=True)
            tries={}
            while True:
                with self.connect() as db:
                    row=db.execute("SELECT id,payload,state,assessment FROM batches WHERE state!='done' ORDER BY id LIMIT 1").fetchone()
                if row is None or row[2]=='failed':return
                batch_id,payload,_,saved=row
                stage='prepare'
                exchange_count=0
                pause=None
                try:
                    values=json.loads(payload)
                    exchange_count=len(values)
                    exchanges=_exchanges(payload)
                    command=historical_relationship_command_id(exchanges)
                    stage='commit'
                    existing=await asyncio.to_thread(command_service.lookup_command,command)
                    if existing is None:
                        if saved:
                            stage='result_validation'
                            values=json.loads(saved);values['relationship_stage']=RelationshipStage(values['relationship_stage']);values['evidence_indexes']=tuple(values['evidence_indexes'])
                            assessment=HistoricalRelationshipAssessment(**values)
                        else:
                            stage='prepare'
                            await self._yield_to_live(busy)
                            current=snapshot()
                            previous={name:getattr(current,name) for name in ('familiarity','trust','comfort','closeness','tension')}
                            previous['relationship_stage'] = current.relationship_stage
                            stage='gateway'
                            assessment=await assess_historical_relationship(exchanges,gateway=gateway,persona_policy=persona_policy,previous_state=previous,preserve_order=True)
                            stage='commit'
                            with self.connect() as db:db.execute('UPDATE batches SET assessment=? WHERE id=?',(json.dumps(asdict(assessment)),batch_id))
                        stage='commit'
                        await asyncio.to_thread(apply_historical_private_world,exchanges,assessment=assessment,command_service=command_service,preserve_order=True)
                    with self.connect() as db:db.execute("UPDATE batches SET state='done',error=NULL,failure_context=NULL WHERE id=?",(batch_id,))
                except asyncio.CancelledError:raise
                except Exception as exc:
                    code=getattr(exc,'code','HISTORY_RELATIONSHIP_FAILED')
                    if not isinstance(code,str) or not code.replace('_','').isalnum():code='HISTORY_RELATIONSHIP_FAILED'
                    transient=transient_failure(code,exc)
                    used=tries.get(batch_id,0)
                    if transient and used<len(self.retry_delays):
                        tries[batch_id]=used+1
                        pause=self.retry_delays[used]
                    else:
                        context=project_history_failure_context(getattr(exc,'failure_context',None))
                        context.setdefault('cause_code','UNKNOWN')
                        context.setdefault('failure_stage',stage)
                        context.setdefault('exception_type',project_history_failure_context({'exception_type':type(exc).__name__}).get('exception_type','OTHER'))
                        context=project_history_failure_context({**context,'batch_id':batch_id,'exchange_count':exchange_count})
                        # Kept beside the projected context (status() projects it away): taken up at the next start.
                        if transient:context['transient']=True
                        with self.connect() as db:db.execute("UPDATE batches SET state='failed',error=?,failure_context=? WHERE id=?",(code,json.dumps(context),batch_id))
                        return
                if pause is not None:
                    await asyncio.sleep(pause)
        finally:self.running=False

    def history_days(self,*,now=None):
        """The days of every assessed exchange whose time is known."""
        with self.connect() as db:
            rows=db.execute("SELECT payload FROM batches WHERE state='done' ORDER BY id").fetchall()
        return exchange_days((exchange for (payload,) in rows for exchange in _exchanges(payload)),now=now)

    def merge_history_days(self,ledger,*,now=None):
        """Count the days of imported letters that were assessed before their days counted.

        Earlier versions applied such an assessment without its days, so a user who moved with weeks of letters
        stayed in the first volume. Once the whole re-read is done, the days missing from the relationship are
        added (see the function below). The state key records the assessed batches it was done for, so it runs
        again only after new batches, and it writes nothing when no day is missing. Returns the days added."""
        state=self.status()
        if state['status']!='APPLIED' or not state['batches']:return 0
        with self.connect() as db:
            marker=json.dumps(db.execute("SELECT COUNT(*),COALESCE(MAX(id),0) FROM batches WHERE state='done'").fetchone())
            stored=db.execute('SELECT value FROM meta WHERE key=?',(_HISTORY_DAYS_KEY,)).fetchone()
        if stored is not None and stored[0]==marker:return 0
        added=add_history_days(ledger,self.history_days(now=now),at=now or datetime.now(timezone.utc))
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',(_HISTORY_DAYS_KEY,marker))
        return added


def add_history_days(ledger,days,*,at):
    """Add the days missing from the relationship as explicit checkpoints; nothing is removed or lowered.

    The state follows the day rule of runtime/relationship_growth.py; letter deletion derives it the same way
    (runtime/imports/letter_delete_replay.py). Returns the number of days added."""
    from runtime.private_world.ledger import LedgerEvent, LedgerVersionConflictError
    from runtime.relationship_growth import history_day_updates
    for attempt in range(3):
        added=0
        try:
            missing=sorted(set(days)-set(ledger.snapshot().relationship_days))
            for offset in range(0,len(missing),_HISTORY_DAYS_PER_EVENT):
                chunk=missing[offset:offset+_HISTORY_DAYS_PER_EVENT]
                identity='history-days.'+hashlib.sha256(json.dumps(chunk).encode('utf-8')).hexdigest()[:40]
                current=ledger.snapshot()
                if ledger.apply_once(LedgerEvent(identity,identity,HISTORY_DAYS_EVENT,{'days':chunk},at.isoformat()),
                        replace(current,version=current.version+1,**history_day_updates(current,chunk)),
                        expected_snapshot_version=current.version):
                    added+=len(chunk)
            return added
        except LedgerVersionConflictError:
            if attempt==2:raise  # a live write won every time; the next start tries again
