"""Canonical boundary changes survive replay and inform the next exchange."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from private_world_ledger import LedgerEvent, SQLitePrivateWorldLedger
from runtime.memory.private_world_delivery import DeliveryEvent, PrivateWorldDeliveryCommitter
from runtime.memory.private_world_relationship import PrivateWorldRelationshipCommitter
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime
from runtime.memory.private_world_projection import project_private_world


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_boundary_set_conflict_withdraw_and_replay(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    committer = PrivateWorldRelationshipCommitter(ledger)
    reply = "以后别叫我小笨蛋，我不喜欢这个称呼。"
    class Model:
        async def complete(self, messages, **kwargs):
            return SimpleNamespace(text=json.dumps({"updates": [], "boundaries": [
                {"action": "set", "boundary_id": None, "quote": reply},
            ]}, ensure_ascii=False))
    runtime = DailyLifeRuntime(store, Model, lambda: "", relationship=ledger.snapshot)
    asyncio.run(runtime.consume_exchange("reply:first:1", "小笨蛋", reply, occurred_at=NOW))
    changes = store.exchange_boundaries("reply:first:1", "小笨蛋", reply)
    assert committer.commit_boundaries("first:1", reply, changes, occurred_at=NOW).value == "REJECTED"
    PrivateWorldDeliveryCommitter(ledger).commit(DeliveryEvent(
        delivery_id="first:1", occurred_at=NOW, semantic_key="first",
        canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
    assert committer.commit_boundaries("first:1", reply, changes, occurred_at=NOW).value == "COMMITTED"
    boundary = ledger.snapshot().active_boundaries[0]
    assert boundary.scope == reply
    assert project_private_world(ledger.snapshot()).behavior.active_boundaries[0].scope == reply
    assert committer.commit_boundaries("first:1", reply, changes, occurred_at=NOW).value == "DUPLICATE"
    assert committer.commit_boundaries("first:1", reply + "另一版", changes, occurred_at=NOW).value == "REJECTED"
    # A fresh process can replay the durable extraction without another LLM call.
    assert DailyLifeStore(tmp_path / "life.sqlite3").exchange_boundaries("reply:first:1", "小笨蛋", reply) == changes

    class Conflict:
        async def complete(self, messages, request_id, **kwargs):
            data = json.loads(messages[-1]["content"])
            assert data["active_boundaries"][0]["boundary_id"] == "b1"
            result = {"conduct": "boundary_violation", "quote": "小笨蛋", "boundary_id": "b1"} if ":conduct" in request_id else {
                "updates": [], "relationship": {"kind": "conflict", "user_quote": "小笨蛋", "reply_quote": "说过不喜欢这个称呼了。"}}
            return SimpleNamespace(text=json.dumps(result, ensure_ascii=False))
    runtime.gateway = Conflict
    assert asyncio.run(runtime.consume_exchange("reply:second:1", "小笨蛋", "说过不喜欢这个称呼了。", occurred_at=NOW + timedelta(minutes=1)))
    assert store.exchange_relationship("reply:second:1", "小笨蛋", "说过不喜欢这个称呼了。")["kind"] == "conflict"

    withdrawal = "以后可以这样叫我了，之前不让你叫的那条作废。"
    class Withdrawal:
        async def complete(self, messages, **kwargs):
            assert json.loads(messages[-1]["content"])["active_boundaries"][0]["boundary_id"] == "b1"
            return SimpleNamespace(text=json.dumps({"updates": [], "boundaries": [
                {"action": "withdraw", "boundary_id": "b1", "quote": withdrawal}]}))
    runtime.gateway = Withdrawal
    asyncio.run(runtime.consume_exchange("reply:third:1", "以后还能这么叫吗？", withdrawal, occurred_at=NOW + timedelta(minutes=2)))
    change = store.exchange_boundaries("reply:third:1", "以后还能这么叫吗？", withdrawal)
    assert change[0]["boundary_id"] == boundary.boundary_id
    PrivateWorldDeliveryCommitter(ledger).commit(DeliveryEvent(
        delivery_id="third:1", occurred_at=NOW + timedelta(minutes=2), semantic_key="third",
        canonical_reply_sha256=hashlib.sha256(withdrawal.encode()).hexdigest()))
    assert committer.commit_boundaries("third:1", withdrawal, change, occurred_at=NOW + timedelta(minutes=2)).value == "COMMITTED"
    assert not ledger.snapshot().active_boundaries
    assert committer.commit_boundaries("third:1", withdrawal, change, occurred_at=NOW + timedelta(minutes=2)).value == "DUPLICATE"
    assert committer.commit_boundaries("first:1", reply, changes, occurred_at=NOW).value == "DUPLICATE"
    assert not ledger.snapshot().active_boundaries
    # An older extraction that was never applied also cannot undo the withdrawal.
    PrivateWorldDeliveryCommitter(ledger).commit(DeliveryEvent(
        delivery_id="late:1", occurred_at=NOW + timedelta(minutes=1), semantic_key="late",
        canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
    assert committer.commit_boundaries("late:1", reply, changes, occurred_at=NOW + timedelta(minutes=1)).value == "REJECTED"
    assert not ledger.snapshot().active_boundaries
    assert ledger.snapshot().relationship_stage == "unknown"
    assert ledger.snapshot().intimacy_grants == ()


@pytest.mark.parametrize("change", [
    {"action": "set", "boundary_id": None, "quote": "不是回信原文"},
    {"action": "withdraw", "boundary_id": "missing", "quote": "以后可以了。"},
    {"action": "grant_intimacy", "boundary_id": None, "quote": "以后可以了。"},
])
def test_invalid_boundary_candidate_does_not_publish(tmp_path, change):
    store = DailyLifeStore(tmp_path / "life.sqlite3")
    class Model:
        async def complete(self, messages, **kwargs):
            return SimpleNamespace(text=json.dumps({"updates": [], "boundaries": [change]}))
    runtime = DailyLifeRuntime(store, Model, lambda: "")
    with pytest.raises(ValueError, match="DAILY_LIFE_BOUNDARY"):
        asyncio.run(runtime.consume_exchange("reply:first:1", "不是回信原文", "以后可以了。", occurred_at=NOW))
    assert not store.has_source("reply:first:1")


def test_old_format_withdrawal_blocks_delayed_boundary_revival(tmp_path):
    ledger = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    reply = "以后别叫我小笨蛋，我不喜欢这个称呼。"
    PrivateWorldDeliveryCommitter(ledger).commit(DeliveryEvent(
        delivery_id="late:1", occurred_at=NOW, semantic_key="late",
        canonical_reply_sha256=hashlib.sha256(reply.encode()).hexdigest()))
    snapshot = ledger.snapshot()
    ledger.apply_once(LedgerEvent(
        event_id="legacy.withdrawal", delivery_id="legacy.command",
        event_type="character_boundary_withdrawn",
        occurred_at=(NOW + timedelta(minutes=2)).isoformat(),
        payload={"applied": True, "reason_code": "CHARACTER_BOUNDARY_WITHDRAWN"},
    ), snapshot, expected_snapshot_version=snapshot.version)
    committer = PrivateWorldRelationshipCommitter(ledger)
    assert committer.commit_boundaries("late:1", reply, [
        {"action": "set", "boundary_id": None, "quote": reply},
    ], occurred_at=NOW).value == "REJECTED"
    assert not ledger.snapshot().active_boundaries


def test_final_letter_wires_boundaries_to_ledger_and_next_prompt(tmp_path):
    script = r'''
import asyncio, json
from types import SimpleNamespace
import local_server as server
from runtime.reply.reply_pipeline import PipelineResult
from reply_orchestrator import ReplyState
from runtime.reply.reply_context import ReplyMode
reply = '以后别叫我小笨蛋，我不喜欢这个称呼。'
class AcceptedReply:
    async def run(self, request, context):
        return PipelineResult('boundary-letter', ReplyState.COMPLETED, text=reply, quality_status='accepted')
class LifeModel:
    calls = 0
    async def complete(self, messages, **kwargs):
        LifeModel.calls += 1
        return SimpleNamespace(text=json.dumps({'updates': [], 'boundaries': [
            {'action':'set','boundary_id':None,'quote':reply}]}))
server.reply_pipeline = AcceptedReply()
server.letters_adapter.gateway = LifeModel()
from runtime.memory.private_world_relationship import RelationshipFactStatus
commit = server.private_world_relationship_committer.commit_boundaries
server.private_world_relationship_committer.commit_boundaries = lambda *args, **kwargs: RelationshipFactStatus.UNAVAILABLE
letter = {'letter_id':'boundary-letter','content':'小笨蛋','reply_text':'','letter_status':'PENDING','reply_mode':ReplyMode.TEXT_LETTER.value}
server.store.letters[:] = [letter]
async def run():
    await server.generate_reply('boundary-letter', letter['content'])
    await asyncio.gather(*tuple(server.daily_life_tasks.values()))
    assert letter['daily_life_status'] == 'PENDING'
    assert not server.private_world_port.snapshot().active_boundaries
    server.private_world_relationship_committer.commit_boundaries = commit
    server._schedule_daily_life_exchange(letter)
    await asyncio.gather(*tuple(server.daily_life_tasks.values()))
    assert LifeModel.calls == 1
    before = server.private_world_port.snapshot()
    server._schedule_daily_life_exchange(letter)
    await asyncio.gather(*tuple(server.daily_life_tasks.values()))
    assert server.private_world_port.snapshot() == before
asyncio.run(run())
context = server.letters_adapter.build_reply_context(ReplyMode.TEXT_LETTER)
assert letter['daily_life_status'] == 'COMMITTED', letter.get('daily_life_error_code')
assert context.private_behavior.active_boundaries[0].scope == reply
assert len(server.private_world_port.snapshot().active_boundaries) == 1
print('boundary_wiring_passed')
'''
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "OLIVIA_LOCAL_DATA_ROOT": str(tmp_path), "OLIVIA_LLM_PROVIDER": "none",
           "OLIVIA_MEMORY_ENABLED": "0", "PYTHONUTF8": "1", "PYTHONPATH": str(root)}
    env.pop("OLIVIA_PRIVATE_WORLD_DB", None)
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "boundary_wiring_passed" in result.stdout
