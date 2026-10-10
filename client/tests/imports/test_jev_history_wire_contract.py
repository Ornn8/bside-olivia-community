"""Synthetic HTTP coverage for the upstream Choice criteria contract."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from private_world_ledger import SQLitePrivateWorldLedger
from private_world_service import PrivateWorldCommandService
from runtime.imports.historical_memory import assess_historical_relationship
from runtime.imports.relationship_batches import RelationshipBatches, archive_exchanges
from runtime.reply import jev_questions
from runtime.reply.jev_semantic_service import decide
from tests.imports.test_relationship_batches import rows


@pytest.fixture
def upstream_choice_contract(monkeypatch):
    calls, reject = [], []

    class ProviderRejected(ValueError):
        pass

    class Native:
        def ask(self, state, questions):
            # TypeSafe ChoiceQuestion descriptions accept text, objects, arrays,
            # or null. Bare numeric/boolean descriptions produce HTTP 422.
            # https://github.com/typesafe-ai/typesafe-sdk-python/blob/main/src/typesafe_sdk/_schemas/models.py
            if any(description is not None and not isinstance(description, (str, dict, list))
                   for question in questions.values()
                   for description in question['criteria'].values()):
                raise ProviderRejected()
            return {key: {'type': 'choice', 'choice':
                         'unknown' if key == 'stage' else 'yes' if key.startswith('e')
                         else '20' if '20' in question['criteria'] else 'up_small'}
                    for key, question in questions.items()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(packet)
            try:
                if reject:
                    raise ProviderRejected()
                value = dict(decide(Native(), packet), backend='jev')
                status = 200
            except ProviderRejected:
                value, status = {'error': 'provider_http_422'}, 422
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(value).encode('utf-8'))

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = jev_questions.JevQuestionsPort(
        f'http://127.0.0.1:{server.server_port}/v1/companion/decide', token='synthetic')
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: port)
    try:
        yield calls, reject
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_five_letter_assessment_satisfies_native_choice_wire_contract(upstream_choice_contract):
    exchanges = archive_exchanges(rows(5))
    result = asyncio.run(assess_historical_relationship(
        exchanges, gateway=object(), persona_policy='Synthetic relationship policy.',
        previous_state={'familiarity': 0}, preserve_order=True))
    assert result.trust == 20 and result.evidence_indexes == (1, 2, 3, 4, 5)
    calls, _reject = upstream_choice_contract
    assert len(calls) == 1 and calls[0]['purpose'] == 'historical-relationship'
    for key in ('familiarity', 'trust', 'comfort', 'closeness', 'tension'):
        assert set(calls[0]['questions'][key]['criteria']) == {str(i) for i in range(101)}


def test_failed_422_batch_resumes_after_explicit_retry_without_reimport(
        tmp_path, upstream_choice_contract):
    calls, reject = upstream_choice_contract
    ledger = SQLitePrivateWorldLedger(tmp_path / 'world.sqlite3')
    service = PrivateWorldCommandService(ledger)
    path = tmp_path / 'queue.sqlite3'
    queue = RelationshipBatches(path)
    queue.enqueue(archive_exchanges(rows(7)))

    def run():
        asyncio.run(queue.run(gateway=object(), persona_policy='Synthetic policy.',
                             command_service=service, snapshot=ledger.snapshot))

    reject.append(True)
    run()
    assert queue.status()['error_code'] == 'PRIVATE_WORLD_HISTORY_JEV_PROVIDER_HTTP_422'
    assert queue.status()['processed'] == 0 and len(calls) == 1
    queue = RelationshipBatches(path)
    run()
    assert len(calls) == 1  # A failed batch never enters an automatic paid retry loop.
    reject.clear()
    queue.retry()
    run()
    assert queue.status()['status'] == 'APPLIED'
    assert queue.status()['processed'] == 7 and queue.status()['error_code'] is None
    assert [len(packet['state']['history']['ordered_exchanges']) for packet in calls] == [5, 5, 2]
    before = ledger.snapshot()
    run()
    assert len(calls) == 3 and ledger.snapshot() == before
