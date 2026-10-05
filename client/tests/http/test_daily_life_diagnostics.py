"""Life diagnostics follow the real API-to-ZIP path without private content."""
import asyncio
import io
import json
import sqlite3
import zipfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from original_client_server import create_original_client_server_runtime
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime

ORIGIN = {"Origin": "https://client.example"}
CONFIRMED = {**ORIGIN, "X-Olivia-Companion-Action": "confirmed"}
PATH = "/toy/companion/private-world/life"


async def fallback(request):
    return web.Response(status=404)


async def exported(client):
    response = await client.get('/toy/diagnostics/export', headers=ORIGIN)
    assert response.status == 200
    with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
        health = json.loads(archive.read('health.json'))
        events = [json.loads(row) for row in archive.read('runtime-tail.jsonl').splitlines()]
        raw = b''.join(archive.read(name) for name in archive.namelist())
    return health['checks']['daily_life'], events, raw


def test_uninitialized_life_is_checked_independently_of_relationship_storage():
    async def run():
        runtime = create_original_client_server_runtime(fallback, trusted_origins=('https://client.example',))
        async with TestClient(TestServer(runtime.app)) as client:
            assert (await client.get(PATH, headers=ORIGIN)).status == 503
            check, events, _ = await exported(client)
            assert check['state'] == 'unavailable'
            assert check['failure_stage'] == 'initialization'
            assert any(row.get('failure_stage') == 'initialization' for row in events)
    asyncio.run(run())


def test_read_error_is_typed_sanitized_exported_and_recovers(tmp_path):
    async def run():
        path = tmp_path / 'private-letter-sk-secret.sqlite3'
        life = DailyLifeRuntime(DailyLifeStore(path), lambda: None, lambda: '[]')
        with sqlite3.connect(path) as db:
            db.execute('ALTER TABLE life_moments RENAME TO unavailable_moments')
        runtime = create_original_client_server_runtime(fallback, daily_life=life, trusted_origins=('https://client.example',))
        async with TestClient(TestServer(runtime.app)) as client:
            assert (await client.get(PATH, headers=ORIGIN)).status == 503
            check, events, raw = await exported(client)
            assert check['state'] == 'unavailable'
            assert check['exception_type'] == 'OperationalError'
            assert check['failure_stage'] == 'read'
            row = next(row for row in events if row.get('event') == 'daily_life_failed')
            assert row['http_status'] == 503 and row['endpoint'] == 'daily_life'
            assert row['sqlite_errorname'] == 'SQLITE_ERROR' and row['sqlite_errorcode'] == sqlite3.SQLITE_ERROR
            assert row['recorded_at_ms'] > 0
            assert b'private-letter' not in raw and b'sk-secret' not in raw and b'C:/Users' not in raw
            assert b'no such table' not in raw and b'life_moments' not in raw
            with sqlite3.connect(path) as db:
                db.execute('ALTER TABLE unavailable_moments RENAME TO life_moments')
            assert (await client.get(PATH, headers=ORIGIN)).status == 200
            check, _, _ = await exported(client)
            assert check['state'] == 'available' and 'error_code' not in check
    asyncio.run(run())


def test_generation_error_does_not_make_read_health_fail_or_run_a_model(tmp_path, monkeypatch):
    async def run():
        def forbidden():
            pytest.fail('diagnostic health must not call a model or adapt a routine')
        life = DailyLifeRuntime(DailyLifeStore(tmp_path / 'life.sqlite3'), forbidden, forbidden)
        life.error_code = 'DAILY_LIFE_GENERATION_UNAVAILABLE'
        monkeypatch.setattr(life.store, 'adapt_routine', lambda *args, **kwargs: forbidden())
        from runtime.private_world.port import PrivateWorldSnapshot
        life.relationship = PrivateWorldSnapshot
        runtime = create_original_client_server_runtime(fallback, daily_life=life, trusted_origins=('https://client.example',))
        async with TestClient(TestServer(runtime.app)) as client:
            check, _, _ = await exported(client)
            assert check['state'] == 'available'
            assert not life._task and not life._emotion
    asyncio.run(run())


@pytest.mark.parametrize('stage,kind', [('request','TypeError'), ('response','SyntaxError'), ('render','RangeError')])
def test_frontend_reports_are_confirmed_allowlisted_and_reach_zip(stage, kind):
    async def run():
        runtime = create_original_client_server_runtime(fallback, trusted_origins=('https://client.example',))
        async with TestClient(TestServer(runtime.app)) as client:
            body = dict(failure_stage=stage, endpoint='daily_life', exception_type=kind, http_status=200)
            assert (await client.post(PATH+'/diagnostic', headers=ORIGIN, json=body)).status == 403
            assert (await client.post(PATH+'/diagnostic', headers={**CONFIRMED, 'Origin':'https://untrusted.example'}, json=body)).status == 403
            assert (await client.post(PATH+'/diagnostic', headers=CONFIRMED, json={**body,'message':'private-secret'})).status == 400
            assert (await client.post(PATH+'/diagnostic', headers=CONFIRMED, json=body)).status == 200
            _, events, raw = await exported(client)
            row = next(row for row in events if row.get('event') == 'daily_life_frontend_failed')
            assert row['failure_stage'] == stage and row['exception_type'] == kind
            assert row['http_status'] == 200 and row['endpoint'] == 'daily_life'
            assert b'private-secret' not in raw
    asyncio.run(run())


def test_initialization_failure_reaches_existing_runtime_log_ring(monkeypatch, capsys):
    from collections import deque
    import local_server
    monkeypatch.setattr(local_server, '_RUNTIME_DIAGNOSTIC_EVENTS', deque(maxlen=160))
    def fail(**kwargs):
        raise PermissionError('private-secret-C:/Users/private')
    monkeypatch.setattr(local_server, 'resolve_private_world_database', fail)
    assert local_server._create_daily_life_runtime() is None
    events = local_server.runtime_diagnostic_event_snapshot()
    assert events[-1]['failure_stage'] == 'initialization'
    assert events[-1]['exception_type'] == 'PermissionError'
    assert events[-1]['recorded_at_ms'] > 0
    assert 'private-secret' not in capsys.readouterr().out


def test_repeated_frontend_reports_do_not_grow_the_export_tail():
    async def run():
        runtime = create_original_client_server_runtime(fallback, trusted_origins=('https://client.example',))
        async with TestClient(TestServer(runtime.app)) as client:
            body = dict(failure_stage='request', endpoint='daily_life', exception_type='Error', http_status=503)
            for _ in range(3):
                assert (await client.post(PATH+'/diagnostic', headers=CONFIRMED, json=body)).status == 200
            _, events, _ = await exported(client)
            assert len([row for row in events if row['event'] == 'daily_life_frontend_failed']) == 1
            for status in range(200, 225):
                assert (await client.post(PATH+'/diagnostic', headers=CONFIRMED,
                    json={**body, 'http_status':status})).status == 200
            _, events, _ = await exported(client)
            reports = [row for row in events if row['event'] == 'daily_life_frontend_failed']
            assert len(reports) == 20 and reports[0]['http_status'] == 205
            assert reports[-1]['http_status'] == 224
            assert (await client.post(PATH+'/diagnostic', headers=CONFIRMED,
                json={**body, 'http_status':True})).status == 400
            assert (await client.post(PATH+'/diagnostic', headers={**CONFIRMED, 'Content-Type':'application/json'},
                data=' '*513)).status == 400
    asyncio.run(run())
