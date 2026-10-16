"""Server consumer/startup refresh projected contact state from temporary SQLite."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace

from runtime.private_world.ledger import LedgerEvent, SQLitePrivateWorldLedger
from runtime.private_world.port import PrivateWorldSnapshot
from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
from runtime.personal_chat.contact_invitation import observe

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def setup_server(tmp_path, monkeypatch):
    import local_server as server
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.db')
    ledger.apply_once(LedgerEvent('old', 'old', 'opaque', {}, NOW.isoformat()),
        PrivateWorldSnapshot(version=2, familiarity=70, trust=69, comfort=69, closeness=70))
    committer = PrivateWorldRelationshipCommitter(ledger)
    for name, at in [('support', NOW + timedelta(minutes=2)), ('conflict', NOW + timedelta(minutes=1))]:
        PrivateWorldDeliveryCommitter(ledger).commit(DeliveryEvent(delivery_id=name, occurred_at=at,
            semantic_key=name, canonical_reply_sha256=hashlib.sha256(b'reply').hexdigest()))
    committer.commit_exchange('support', 'support', 'reply',
        {'kind': 'support_received', 'user_quote': 'support', 'reply_quote': 'reply'},
        occurred_at=NOW + timedelta(minutes=2))
    original = {'letter_id': 'support', 'private_world_delivery_id': 'support',
                'letter_status': 'COMPLETED', 'content': 'support'}
    observe(original, ledger.snapshot(), ledger.events())
    assert original['contact_qualification'] is True
    late = {'letter_id': 'conflict', 'reply_revision': 1, 'private_world_delivery_id': 'conflict',
            'private_world_occurred_at': (NOW + timedelta(minutes=1)).isoformat(),
            'letter_status': 'COMPLETED', 'content': 'conflict', 'reply_text': 'reply'}
    chats = [dict(original, letter_id='qq-support', channel='qq')]
    monkeypatch.setattr(server, 'store', SimpleNamespace(letters=[original, late], personal_chats=chats))
    monkeypatch.setattr(server, 'private_world_port', ledger)
    monkeypatch.setattr(server, 'private_world_relationship_committer', committer)
    monkeypatch.setattr(server, 'daily_life_tasks', {})
    saved = []
    monkeypatch.setattr(server, '_persist_store_state', lambda: saved.append(True))
    async def consume(*args, **kwargs):
        pass  # Fixed extraction; all submission, storage and observation are real.
    world = SimpleNamespace(consume_exchange=consume, schedule_refresh=lambda now: None,
        store=SimpleNamespace(exchange_relationship=lambda *a, **k: {
            'kind': 'conflict', 'user_quote': 'conflict', 'reply_quote': 'reply'},
            exchange_boundaries=lambda *a, **k: []))
    monkeypatch.setattr(server, 'daily_life_runtime', world)
    return server, ledger, committer, original, late, chats[0], saved


def test_scheduled_consumer_refreshes_old_letter_and_qq_row(tmp_path, monkeypatch):
    server, ledger, _, original, late, chat, saved = setup_server(tmp_path, monkeypatch)
    async def run():
        server._schedule_daily_life_exchange(late)
        await asyncio.gather(*tuple(server.daily_life_tasks.values()))
        assert original['contact_qualification'] is True  # Live extraction does not settle growth.
        from runtime.relationship_batch import BatchQueue, tick
        queue=BatchQueue(ledger._database_path)
        queue.sync(server.store.letters,NOW)
        async def judge(gateway,batch,chosen):
            return {'turns':[{'id':'conflict','signals':{'rel_friction':'conflict'},
                             'moment':'rel_friction:conflict','user_quote':'conflict','reply_quote':'reply'}]}
        assert await tick(server,NOW+timedelta(days=2),judge)=='updated'
    asyncio.run(run())
    assert late['daily_life_status'] == 'COMMITTED'
    assert original['contact_qualification'] is False
    assert chat['contact_qualification'] is False
    assert saved


def test_startup_refreshes_persisted_rows_and_degrades_when_ledger_unavailable(tmp_path, monkeypatch):
    import runtime.image_reply
    server, ledger, committer, original, late, chat, saved = setup_server(tmp_path, monkeypatch)
    committer.commit_exchange('conflict', 'conflict', 'reply',
        {'kind': 'conflict', 'user_quote': 'conflict', 'reply_quote': 'reply'},
        occurred_at=NOW + timedelta(minutes=1))
    monkeypatch.setattr(server, '_refresh_proactive_context', lambda: None)
    monkeypatch.setattr(server, '_proactive_settings', lambda: {'enabled': False})
    monkeypatch.setattr(server, '_schedule_pending_reply_jobs', lambda: None)
    monkeypatch.setattr(server, '_schedule_pending_media_jobs', lambda: None)
    monkeypatch.setattr(runtime.image_reply, 'schedule', lambda *a, **k: None)
    monkeypatch.setattr(server, 'media_tasks', set())
    async def idle():
        pass
    monkeypatch.setattr(server, '_proactive_loop', idle)
    monkeypatch.setattr(server, '_recover_photo_memories', idle)
    logs = []
    monkeypatch.setattr(server, '_safe_log', lambda code, **kw: logs.append(code))
    async def start():
        await server._start_reply_tasks(None)
        for task in tuple(server.media_tasks):
            task.cancel()
        await asyncio.gather(*tuple(server.media_tasks), return_exceptions=True)
        if server._proactive_task:
            await server._proactive_task
    asyncio.run(start())
    assert original['contact_qualification'] is False
    assert chat['contact_qualification'] is False
    assert saved
    def unavailable():
        raise OSError('private failure detail')
    monkeypatch.setattr(ledger, 'events', unavailable)
    asyncio.run(start())
    assert 'contact_projection_refresh_unavailable' in logs
    assert all('private failure detail' not in line for line in logs)
