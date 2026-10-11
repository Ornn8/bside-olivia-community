"""A valid abstention completes an import batch without changing relationships."""
import asyncio
import json

import pytest

from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import PrivateWorldCommandService
from runtime.imports.historical_memory import (
    HistoricalRelationshipError, assess_historical_relationship,
)
from runtime.imports.relationship_batches import RelationshipBatches, archive_exchanges
from runtime.reply import jev_questions
from tests.imports.test_relationship_batches import rows


class NoEvidence:
    def __init__(self):
        self.calls = []

    async def ask(self, state, questions, *, purpose):
        self.calls.append(state)
        # Unsupported scores/stage must never mutate state, even if confident.
        return {key: 'close' if key == 'stage' else 'no' if key.startswith('e')
                else '100' if '100' in question['criteria'] else 'up_big'
                for key, question in questions.items()}


def test_all_no_evidence_batches_complete_and_recover_without_state_changes(tmp_path, monkeypatch):
    port = NoEvidence()
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')
    service = PrivateWorldCommandService(ledger)
    before = ledger.snapshot()
    exchanges = archive_exchanges(rows(7))
    path = tmp_path / 'queue.sqlite3'
    queue = RelationshipBatches(path)
    queue.enqueue(exchanges)

    def run():
        asyncio.run(queue.run(gateway=object(), persona_policy='Synthetic policy.',
                             command_service=service, snapshot=ledger.snapshot))

    run()
    assert queue.status()['status'] == 'APPLIED'
    assert queue.status()['processed'] == 7
    assert [len(state['history']['ordered_exchanges']) for state in port.calls] == [5, 2]
    assert ledger.snapshot() == before
    with queue.connect() as db:
        saved = db.execute('SELECT assessment FROM batches ORDER BY id').fetchall()
        assert all(json.loads(row[0])['evidence_indexes'] == [] for row in saved)

    queue = RelationshipBatches(path)
    queue.enqueue(exchanges)
    run()
    run()
    assert len(port.calls) == 2 and ledger.snapshot() == before
    # Recover a crash after saving the abstention but before completing its row.
    with queue.connect() as db:
        db.execute("UPDATE batches SET state='pending' WHERE id=1")
    run()
    assert queue.status()['processed'] == 7
    assert len(port.calls) == 2 and ledger.snapshot() == before
    # A later supported batch still commits normally after an abstention.
    async def supported(state, questions, *, purpose):
        port.calls.append(state)
        # Ordered batches judge the change from the running state, toward the history ceiling.
        return {key: 'familiar' if key == 'stage' else 'yes' if key.startswith('e') else 'up'
                for key in questions}
    port.ask = supported
    queue.enqueue(archive_exchanges(rows(8)))
    run()
    assert queue.status()['processed'] == 8 and len(port.calls) == 3
    assert ledger.snapshot().trust == before.trust + max(0, round(0.2 * (60 - before.trust)))


@pytest.mark.parametrize('invalid', ['outside', 'duplicate', 'too_many'])
def test_malformed_evidence_is_still_rejected(monkeypatch, invalid):
    class Gateway:
        async def complete(self, messages, **kwargs):
            from types import SimpleNamespace
            indexes = {'outside': [99], 'duplicate': [1, 1], 'too_many': list(range(1, 10))}[invalid]
            return SimpleNamespace(text=json.dumps(dict(relationship_stage='unknown',
                familiarity=0, trust=0, comfort=0, closeness=0, tension=0, evidence_indexes=indexes)))
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: None)
    with pytest.raises(HistoricalRelationshipError, match='PRIVATE_WORLD_HISTORY_RESULT_INVALID'):
        asyncio.run(assess_historical_relationship(archive_exchanges(rows(10)),
            gateway=Gateway(), persona_policy='Synthetic policy.'))
