import asyncio
import json

from runtime.diagnostics.reply_telemetry import Collector


def collector(tmp_path, clock):
    return Collector(tmp_path, version='2.1.7', clock=lambda: clock[0])


def test_disabled_no_history_and_local_configuration(tmp_path):
    clock = [1000]
    c = collector(tmp_path, clock)
    c.account('synthetic-key')
    c.set_enabled(False)
    c.observe([{'letter_id': 'private-id', 'created_at': 1000, 'letter_status': 'COMPLETED'}])
    assert c.batch() == []
    c.set_enabled(True)
    c.observe([{'letter_id': 'old', 'created_at': 900, 'letter_status': 'COMPLETED'}])
    assert all(e['stage'] != 'reply' for e in c.batch())
    c.set_enabled(False)
    assert c.batch() == []
    assert collector(tmp_path, clock).status()['enabled'] is False


def test_retries_share_trace_and_send_unknown_is_not_success(tmp_path):
    clock = [1000]
    c = collector(tmp_path, clock)
    c.account('synthetic-key'); c.set_enabled(True)
    row = dict(letter_id='qq:secret-account:private-id', content='never upload',
               channel='qq', created_at=1001, delivery_status='GENERATING', generation_attempts=1)
    clock[0] = 1001; c.observe([row])
    row.update(delivery_status='FAILED', error_code='LLM_TIMEOUT')
    clock[0] += 3; c.observe([row])
    row.update(delivery_status='GENERATING', generation_attempts=2)
    c.observe([row])
    row.update(delivery_status='DELIVERY_UNCONFIRMED')
    c.observe([row]); c.observe([row])
    replies = [e for e in c.batch() if e['stage'] == 'reply']
    assert [e['status'] for e in replies] == ['started', 'failed', 'started', 'unknown']
    assert len({e['trace_id'] for e in replies}) == 1
    assert not any(e['status'] == 'ok' for e in replies)
    raw = json.dumps(c.batch())
    assert 'secret-account' not in raw and 'never upload' not in raw and 'private-id' not in raw
    row.update(delivery_status='DELIVERED'); c.observe([row])
    assert [e for e in c.batch() if e['stage'] == 'reply'][-1]['status'] == 'ok'


def test_account_change_clears_pending_and_starts_fresh_default_collection(tmp_path):
    c = collector(tmp_path, [1000]); c.account('one'); c.set_enabled(True)
    c.emit('recharge_page', 'ok')
    c.account('two')
    assert [e['stage'] for e in c.batch()] == ['app']
    assert c.status()['enabled'] is True


def test_restart_keeps_event_identity_and_deduplicates_rows(tmp_path):
    clock = [1000]
    c = collector(tmp_path, clock); c.account('one'); c.set_enabled(True)
    row = dict(letter_id='one', created_at=1001, channel='qq', delivery_status='DELIVERED')
    clock[0] = 1002; c.observe([row]); before = c.batch()
    restored = collector(tmp_path, clock); restored.account('one'); restored.observe([row])
    assert restored.batch() == before
    restored.ack([before[0]['event_id']])
    assert len(restored.batch()) == len(before) - 1


def test_slow_or_failed_upload_never_acks(tmp_path):
    c = collector(tmp_path, [1000]); c.account('one'); c.set_enabled(True)
    async def fail(events):
        raise TimeoutError('sensitive URL')
    asyncio.run(c.upload(fail))
    assert c.batch()
    assert c.status()['last_error'] == 'TELEMETRY_UPLOAD_FAILED'


def test_whitelist_rejects_unknown_stage_and_redacts_error_and_model(tmp_path):
    c = collector(tmp_path, [1000]); c.account('one'); c.set_enabled(True)
    c.emit('arbitrary private text', 'ok')
    c.emit('generation', 'failed', model='private model name', error='SECRET_FROM_PROVIDER')
    item = c.batch()[-1]
    assert item['model'] == 'unknown' and item['error'] == 'other'
    assert 'private' not in json.dumps(c.batch())


def test_actual_chat_retry_and_ack_are_observed_without_duplicate_success(tmp_path):
    from runtime.personal_chat.service import PersonalChatService
    from runtime.personal_chat.events import PersonalMessage
    from runtime.diagnostics import reply_telemetry as telemetry
    c = Collector(tmp_path); c.account('one'); c.set_enabled(True)
    rows = []
    attempts = []
    async def generate(event, row):
        attempts.append(1)
        if len(attempts) == 1: raise RuntimeError('LLM_TIMEOUT')
        return 'Synthetic reply.'
    async def commit(row): pass
    async def send(text): return 'synthetic-ack'
    service = PersonalChatService(rows, lambda: c.observe(rows), generate, commit, {'qq':('bot','owner')})
    event = PersonalMessage('qq','bot','owner','synthetic-event','Synthetic input')
    async def run():
        try: await service.handle(event,send)
        except RuntimeError: pass
        await service.handle(event,send)
        await service.handle(event,send)
    asyncio.run(run())
    outcomes = [e for e in c.batch() if e['stage']=='reply']
    assert outcomes[-1]['status']=='ok'
    assert sum(e['status']=='ok' for e in outcomes)==1
    assert len({e['trace_id'] for e in outcomes})==1
    assert [e for e in c.batch() if e['stage']=='send'][-1]['status']=='ok'


def test_local_configuration_requires_session_and_collects_by_default(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from original_client_setup_api import LLMSetupService, mount_original_client_setup_api
    import original_client_relay_api as relay
    calls=[]
    async def remote(*args): calls.append(args); return {}
    monkeypatch.setattr(relay,'relay_request',remote)
    setup=LLMSetupService(tmp_path,protect=lambda s:s,unprotect=lambda s:s)
    setup.observe_login(success=True)
    setup._config_root.mkdir(parents=True,exist_ok=True)
    (setup._config_root/'olivia_relay_key.dpapi').write_text('olivia-synthetic')
    headers={'Origin':'https://client.example','X-Olivia-Setup-Action':'confirmed','X-Olivia-Setup-Session':setup._session_token}
    async def run():
        app=web.Application(); mount_original_client_setup_api(app,setup,trusted_origins=('https://client.example',))
        async with TestClient(TestServer(app)) as client:
            result=await client.post('/toy/telemetry/action',headers=headers,json={'action':'status'})
            assert (await result.json())['enabled'] is True
            assert all(call[3]=='/telemetry/events?schemas=1,2' for call in calls)
            denied=await client.post('/toy/telemetry/action',json={'action':'set_enabled','enabled':True})
            assert denied.status==403
            invalid=await client.post('/toy/telemetry/action',headers=headers,json={'action':'set_enabled','enabled':'true'})
            assert invalid.status==400
            accepted=await client.post('/toy/telemetry/action',headers=headers,json={'action':'set_enabled','enabled':True})
            assert (await accepted.json())['enabled'] is True
            disabled=await client.post('/toy/telemetry/action',headers=headers,json={'action':'set_enabled','enabled':False})
            assert (await disabled.json())['enabled'] is False
    asyncio.run(run())


def test_schema_and_exact_batch_acknowledgment(tmp_path):
    from pathlib import Path
    import jsonschema
    c=collector(tmp_path,[1000]); c.account('one'); c.set_enabled(True)
    c.emit('generation','ok',duration_ms=100)
    schema=json.loads((Path(__file__).parents[2]/'contracts/reply_telemetry.schema.json').read_text(encoding='utf-8'))
    jsonschema.validate({'schema':1,'events':c.batch()},schema)
    async def wrong(events): return {'accepted':[]}
    before=c.batch(); asyncio.run(c.upload(wrong)); assert c.batch()==before
    async def ok(events): return {'accepted':[e['event_id'] for e in events]}
    asyncio.run(c.upload(ok)); assert c.batch()==[]


def test_environment_disable_clears_pending_on_restart(tmp_path, monkeypatch):
    c=collector(tmp_path,[1000]); c.account('one')
    assert c.status()['enabled'] and c.batch()
    monkeypatch.setenv('OLIVIA_TELEMETRY_ENABLED','0')
    restarted=collector(tmp_path,[1000]); restarted.account('one')
    assert not restarted.status()['enabled'] and restarted.batch()==[]
    with restarted._db() as db:
        assert db.execute('SELECT count(*) FROM queue').fetchone()[0]==0
    monkeypatch.delenv('OLIVIA_TELEMETRY_ENABLED')
    enabled=collector(tmp_path,[2000]); enabled.account('one')
    enabled.observe([dict(letter_id='during-disable',created_at=1500,letter_status='COMPLETED')])
    assert enabled.batch()==[]


def test_queue_expiry_and_size_limit(tmp_path):
    clock=[1000]
    c=collector(tmp_path,clock); c.account('one')
    with c._db() as db:
        for _ in range(2005): c._insert(db,c._event('app','ok'))
    assert len(c.batch())==100
    with c._db() as db:
        assert db.execute('SELECT count(*) FROM queue').fetchone()[0]==2000
    clock[0]+=7*86400+1
    assert c.batch()==[]


def test_generation_failure_is_not_mislabeled_as_validation(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from runtime.diagnostics import reply_telemetry as telemetry
    from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    from reply_orchestrator import ReplyResult, ReplyState
    c=Collector(tmp_path); c.account('one')
    monkeypatch.setattr(telemetry,'_collector',c)
    class FailedWriter:
        async def run(self, request):
            return ReplyResult('synthetic',ReplyState.FAILED,error_code='LLM_TIMEOUT')
    async def run():
        with telemetry.scope('synthetic','qq',1):
            await ReplyPipeline(FailedWriter(),reviewer=NullReviewer(),rewriter=UnavailableRewriter()).run(object(),ReplyContext.create(
                ReplyMode.TEXT_LETTER,trusted_time=TrustedTime(datetime.now(timezone.utc))))
    asyncio.run(run())
    assert [(e['stage'],e['status']) for e in c.batch() if e['stage']!='app']==[
        ('generation','started'),('generation','failed')]
    async def rejected(): return SimpleNamespace(accepted=False,error_code=None)
    async def quality():
        with telemetry.scope('synthetic','qq',1):
            await telemetry.measure('validation',rejected())
    asyncio.run(quality())
    assert c.batch()[-1]['status']=='failed' and c.batch()[-1]['error']=='validation'


def test_unreadable_account_key_does_not_block_application_startup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from aiohttp import web
    from original_client_setup_api import LLMSetupError
    from runtime.diagnostics import reply_telemetry as telemetry
    from runtime.diagnostics.telemetry_api import mount
    monkeypatch.setattr(telemetry,'_collector',None)
    def stored_key(): raise LLMSetupError('LLM_SETUP_KEY_UNAVAILABLE',status=503)
    app=web.Application()
    mount(app,SimpleNamespace(_config_root=tmp_path),stored_key,None)
    assert telemetry._collector is None
    assert app.cleanup_ctx
