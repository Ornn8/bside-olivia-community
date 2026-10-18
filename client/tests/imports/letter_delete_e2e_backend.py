"""A real local backend for the letter deletion end-to-end test. Synthetic data only.

Run as a script by ``test_letter_delete_end_to_end.py`` and ``test_letter_delete_end_to_end_browser.py``::

    python letter_delete_e2e_backend.py --root DIR --port PORT --jev-port PORT [--mem0-site DIR] [--script FILE]

The process is the one the installed app starts: the real ``local_server`` module and the real original-client
application around it (``create_configured_original_client_server_runtime``), configured the way the launcher
configures it (data root, Mem0 memory, private world ledger, daily life store). Only the boundaries that would leave
the computer or need a model are replaced, each by a deterministic stand-in that is as small as it can be:

* the Jev semantic service: a loopback HTTP service answering the memory-extraction questions;
* the Mem0 embedder: a hashed character-bigram vector instead of a downloaded model (the vector store is the real
  local Qdrant store, and the real ``mem0.Memory`` class writes it);
* the reply pipeline: scripted reply text for the text each letter carries (the rest of the letter flow is real);
* the daily life extraction model, the relationship batch relay and the assessment of imported old letters:
  scripted answers;
* the private world candidate analyzer.

Nothing may leave the computer: the process refuses every connection to a name that is not loopback and writes the
name down in ``network-blocked.log``, which the tests expect to stay empty.

Everything else (letter routes, the outbox that writes memories and the original-text index, daily life storage,
private world ledger, the relationship batch writer, the deletion routes) is the product code. Extra routes under
``/__e2e__/`` let the test settle background work and read the stores back as normalized rows.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

CLIENT = Path(__file__).resolve().parents[2]
DIMENSIONS = 512


# --------------------------------------------------------------------------------------------------------------
# Stand-ins for the boundaries that leave the computer
# --------------------------------------------------------------------------------------------------------------
def block_external_network(log_path: Path) -> None:
    """Nothing may leave the computer: any name that is not loopback is refused and written down."""
    import ipaddress
    import socket

    def note(host) -> None:
        with open(log_path, 'a', encoding='utf-8') as log:
            log.write(f'{host}\n')

    def local(host) -> bool:
        if host in (None, '', 'localhost'):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    real_getaddrinfo, real_connect, real_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex

    def getaddrinfo(host, *args, **kwargs):
        if not local(host):
            note(host)
            raise socket.gaierror(socket.EAI_NONAME, 'blocked: synthetic test')
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):
        if isinstance(address, tuple) and not local(address[0]):
            note(address[0])
            raise OSError('blocked: synthetic test')
        return real_connect(self, address)

    def connect_ex(self, address):
        if isinstance(address, tuple) and not local(address[0]):
            note(address[0])
            return 10061
        return real_connect_ex(self, address)

    socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = getaddrinfo, connect, connect_ex


def embed(text: str) -> list[float]:
    """A deterministic unit vector: character bigrams hashed into a fixed number of slots."""
    vector = [0.0] * DIMENSIONS
    text = text or ' '
    for index in range(max(1, len(text) - 1)):
        gram = text[index:index + 2]
        slot = int.from_bytes(hashlib.blake2b(gram.encode('utf-8'), digest_size=4).digest(), 'big') % DIMENSIONS
        vector[slot] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def start_jev(port: int) -> ThreadingHTTPServer:
    """The Jev semantic service on loopback. Memory extraction keeps every sentence; nothing else is offered."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            packet = json.loads(body)
            # Memory extraction keeps every sentence; the exchange gate for the world needs no update.
            answer = {'memory-extraction': 'keep', 'exchange-world-update': 'none'}.get(packet.get('purpose'))
            if not self.path.endswith('/semantic-decisions') or answer is None:
                self.send_response(503)
                self.end_headers()
                return
            decisions = {key: answer for key in packet['questions']}
            payload = json.dumps({'backend': 'jev', 'input_digest': hashlib.sha256(body).hexdigest(),
                                  'decisions': decisions}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, name='e2e-jev', daemon=True).start()
    return server


def patch_memory():
    from runtime.memory import mem0_memory
    # The product must be the one that imports mem0 (it refuses a module that was imported another way).
    mem0_memory._load_product_mem0_module()
    from mem0.embeddings.base import EmbeddingBase as embedding_base
    from mem0.utils.factory import EmbedderFactory

    class Embedder(embedding_base):
        def embed(self, text, memory_action=None):
            return embed(text)

    EmbedderFactory.create = classmethod(lambda cls, *args, **kwargs: Embedder())
    mem0_memory.verified_embedding_cache = lambda config: True


class Script:
    """What the stand-ins answer, keyed by the text of a letter. Loaded from the test's JSON file."""

    def __init__(self, path: str | None):
        data = json.loads(Path(path).read_text(encoding='utf-8')) if path else {}
        self.exchanges = data.get('exchanges', {})

    def entry(self, user_text: str) -> dict:
        return self.exchanges.get(user_text, {})


def patch_stand_ins(server, script: Script, root: Path):
    from runtime.reply.reply_orchestrator import ReplyState

    class Pipeline:
        async def run(self, request, context):
            content = request.received_user_text or request.content
            entry = script.entry(content)
            return SimpleNamespace(
                request_id=request.request_id, state=ReplyState.COMPLETED, text=entry.get('reply', '收到你的信了。'),
                error_code=None, retryable=False, failure_context={}, quality_status=None, reviewer_calls=0,
                rewrite_calls=0, degraded_stages={}, companion_decision=None, companion_timing=None,
                companion_delivery=None, expression_context=None, sticker_id=None, signature=None,
                stage_timing_seconds={}, stage_cache_hits={}, stage_actual_calls={})

    server.reply_pipeline = Pipeline()

    # Daily life: the extraction model's answer for one exchange.
    from runtime.private_world.daily_life_runtime import DailyLifeRuntime

    async def complete(self, prompt, data, request_id, **kwargs):
        updates = [dict(item) for item in script.entry(data.get('user_letter', '')).get('life', [])]
        return {'updates': updates, 'current_quote': None, 'relationship': None, 'routine': None,
                'boundaries': [], 'contact_choice': None}

    DailyLifeRuntime._complete = complete
    # The model-backed duties of the Jev service (character development) are not part of this test.
    import runtime.private_world.daily_life_runtime as daily_life_module
    daily_life_module.configured_duties = lambda: None

    # Private world candidates: one scripted proposal per letter that asks for it.
    from private_world_candidate import PrivateWorldCandidateProposal
    from private_world_candidates import CandidateType

    class Analyzer:
        async def analyze(self, request):
            entry = script.entry(request.user_excerpt)
            if not entry.get('candidate'):
                return None
            return PrivateWorldCandidateProposal(CandidateType(entry['candidate']['type']), 0.9, entry['candidate']['summary'])

    server.private_world_candidate_analyzer = Analyzer()

    # Her diary: the writer's answer is an entry that quotes every message of the day it was given.
    from runtime.diary import diary as diary_module

    async def write_diary(gateway, request_id, system, packet, response_format, timeout, *, model):
        if '-revise:' in request_id or request_id.startswith('diary-memoir'):
            raise RuntimeError('DIARY_WRITER_UNAVAILABLE')  # the draft stands
        said = ' / '.join(part for m in packet['messages'] for part in (m['user'], m['linli']) if part)
        return {'title': '重写的日记', 'mood': '平静', 'body': '那天的信：' + said, 'facts': []}

    diary_module._complete = write_diary

    # Imported old letters: the model's judgment of a batch of five exchanges, and the switch that lets it run.
    from runtime.imports import relationship_batches as history_batches
    from runtime.imports.historical_memory import HistoricalRelationshipAssessment
    from runtime.reply.reply_context import RelationshipStage

    async def assess(exchanges, **kwargs):
        return HistoricalRelationshipAssessment(RelationshipStage.FAMILIAR, 30, 20, 20, 10, 0,
                                                tuple(range(1, len(exchanges) + 1)))

    history_batches.assess_historical_relationship = assess
    # The assessment runs only once the test switches it on (by creating this file), as it would once the service is connected.
    server._llm_runtime_ready = lambda config=None: (root / 'history-judge').exists()


# --------------------------------------------------------------------------------------------------------------
# Read-back of every store, as normalized rows
# --------------------------------------------------------------------------------------------------------------
def dump_database(path: Path) -> dict[str, list]:
    """Every table of a SQLite file as sorted rows; virtual-table shadow tables are skipped."""
    if not path.exists():
        return {}
    result: dict[str, list] = {}
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=10) as db:
        db.row_factory = sqlite3.Row
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            if any(name.endswith(suffix) for suffix in ('_data', '_idx', '_docsize', '_config', '_content')) \
                    and name.rsplit('_', 1)[0] in names:
                continue
            try:
                rows = [{k: ('blob:' + hashlib.sha256(v).hexdigest() if isinstance(v, bytes) else v)
                         for k, v in dict(r).items()}
                        for r in db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')]
            except sqlite3.Error:
                continue
            result[name] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=repr))
    return result


def qdrant_points(server) -> list[dict]:
    adapter = server.conversation_memory_adapter
    with adapter._using_current() as current:
        store = getattr(getattr(current, 'backend', None), 'vector_store', None)
        if store is None:
            return []
        points, offset = [], None
        while True:
            batch, offset = store.client.scroll(collection_name=store.collection_name, offset=offset, limit=256,
                                                with_payload=True, with_vectors=False)
            points.extend({'id': str(p.id), 'payload': p.payload} for p in batch)
            if offset is None:
                break
        return sorted(points, key=lambda p: p['id'])


def snapshot(server, root: Path) -> dict:
    data = root / 'data'
    stores = {
        'archive': data / 'memory' / 'memory.sqlite3',
        'originals': data / 'memory' / 'mem0' / 'original-text-index.sqlite3',
        'history': data / 'memory' / 'mem0' / 'history' / 'history.db',
        'journal': data / 'memory' / 'mem0' / 'delivery.sqlite3',
        'imported_relationship': data / 'history-relationship.sqlite3',
        'life': data / 'private_world' / 'daily_life.sqlite3',
        'world': data / 'private_world' / 'private_world.sqlite3',
        'diary': data / 'private_world' / 'diary.sqlite3',
    }
    databases = {name: dump_database(path) for name, path in stores.items()}
    shown = ('letter_id', 'content', 'reply_text', 'letter_status', 'created_at', 'reply_revision',
             'private_world_delivery_id', '_deleted_parts', 'daily_life_status', 'daily_life_error_code',
             'daily_life_failure_reason', 'private_world_status', 'candidate_analysis_status')
    letters = [{key: row.get(key) for key in shown} for row in server.store.letters]
    # Every field of every letter row, as text, to look for words of a deleted letter that a field might repeat.
    rows_text = [json.dumps(row, ensure_ascii=False, sort_keys=True, default=repr) for row in server.store.letters]
    state_path = data / 'state.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
        state.pop('letter_trash', None)
        state_text = json.dumps(state, ensure_ascii=False, sort_keys=True)
    except (OSError, ValueError):
        state_text = ''
    return {'letters': letters, 'letter_rows_text': rows_text, 'state_text': state_text,
            'databases': databases, 'points': qdrant_points(server),
            'trash': [{'id': key, 'letter_id': v['letter_id'], 'part': v['part'], 'status': v['status']}
                      for key, v in server.store.letter_trash.items()]}


# --------------------------------------------------------------------------------------------------------------
# Control routes
# --------------------------------------------------------------------------------------------------------------
def unfinished_quotes(root: Path) -> int:
    """Relationship quotes of the letters sent through the app that the background worker has not judged yet."""
    path = root / 'data' / 'memory' / 'mem0' / 'original-text-index.sqlite3'
    if not path.exists():
        return 0
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=10) as db:
        try:
            return db.execute("SELECT COUNT(*) FROM relationship_quotes WHERE categories IS NULL AND source LIKE 'reply:%'").fetchone()[0]
        except sqlite3.Error:
            return 0


async def settle(server, root: Path, *, timeout: float = 60.0) -> dict:
    """Wait until the background work started by the letters so far has finished."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = [t for t in tuple(server.daily_life_tasks.values()) if not t.done()]
        pending += [t for t in tuple(server.private_world_candidate_tasks) if not t.done()]
        runtime = server.conversation_memory_runtime_status()
        completed = [row for row in server.store.letters if row.get('letter_status') == 'COMPLETED']
        sending = [row for row in server.store.letters
                   if row.get('letter_status') in {'PENDING', 'PROCESSING', 'SENDING'}]
        life_done = all(row.get('daily_life_status') == 'COMMITTED' or row.get('_deleted_parts') for row in completed)
        memory_done = runtime.terminal_count >= len(completed) and runtime.pending_count == 0 and not runtime.delivery_pending
        if not pending and not sending and life_done and memory_done and not unfinished_quotes(root):
            return {'settled': True, 'terminal': runtime.terminal_count, 'status': runtime.status}
        await asyncio.sleep(0.25)
    runtime = server.conversation_memory_runtime_status()
    return {'settled': False, 'pending': runtime.pending_count, 'terminal': runtime.terminal_count,
            'status': runtime.status, 'reason': runtime.reason_code}


def relationship_request(script: Script):
    async def request(gateway, batch, chosen):
        turns = []
        for turn in batch['turns']:
            entry = script.entry(turn['user'])
            signal = entry.get('relationship')
            if not signal:
                turns.append({'id': turn['id'], 'signals': {}, 'moment': None})
            else:
                turns.append({'id': turn['id'], 'signals': signal['signals'], 'moment': signal['moment'],
                              'user_quote': signal['user_quote'], 'reply_quote': signal['reply_quote']})
        return {'turns': turns}
    return request


def e2e_middleware(server, root: Path, script: Script):
    from aiohttp import web

    @web.middleware
    async def middleware(request, handler):
        if not request.path.startswith('/__e2e__/'):
            return await handler(request)
        name = request.path.rsplit('/', 1)[-1]
        if request.path.startswith('/__e2e__/static/'):
            # The page of the browser test: a stand-in for the app's own page, served from loopback like the app's.
            file = root / 'static' / name
            if not file.is_file():
                return web.Response(status=404)
            return web.Response(body=file.read_bytes(),
                                content_type='text/html' if name.endswith('.html') else 'application/javascript')
        if name == 'ping':
            return web.json_response({'ok': True})
        if name == 'memory_ready':
            status = server.conversation_memory_adapter.status()
            return web.json_response({'status': status.status, 'reason': getattr(status, 'reason_code', None),
                                      'ready': bool(server._conversation_memory_ready_for_reply()),
                                      'life': server.daily_life_runtime is not None,
                                      'candidates': server.private_world_candidate_store is not None})
        if name == 'settle':
            return web.json_response(await settle(server, root))
        if name == 'snapshot':
            return web.json_response(await asyncio.to_thread(snapshot, server, root))
        if name == 'relationship_tick':
            from datetime import datetime, timedelta, timezone
            from runtime import relationship_batch
            result = await relationship_batch.tick(server, now=datetime.now(timezone.utc) + timedelta(days=2),
                                                   request=relationship_request(script))
            return web.json_response({'result': result})
        if name == 'rescan':
            runtime = server.conversation_memory_runtime_status()
            from runtime.memory import conversation_memory_runtime as cmr
            outbox = cmr._RUNTIME.outbox if cmr._RUNTIME else None
            if outbox is not None:
                await outbox.scan_once()
            return web.json_response({'rescanned': outbox is not None, 'status': runtime.status})
        if name == 'diary_tick':
            from datetime import datetime, timedelta, timezone
            ahead = (await request.json()).get('days_ahead', 2)
            await server._diary_tick(datetime.now(timezone.utc) + timedelta(days=ahead))
            return web.json_response({'entries': {e['day']: e['body'] for e in
                                                  (server.diary_store.entry(d) for d in server.diary_store.days()) if e}})
        if name in {'diary_add', 'diary_clear'}:
            from datetime import datetime, timezone
            from runtime.diary.diary import local_day
            day = local_day(datetime.now(timezone.utc))
            text = (await request.json()).get('text', '') if request.can_read_body else ''
            with server.diary_store._db() as db:
                if name == 'diary_add':
                    # her diary for today, the memoir of this month, and a comment under the day
                    for key, title in ((day, '合成日记'), (day[:7], '合成月度回忆')):
                        db.execute('INSERT OR REPLACE INTO diary_entries(day,written_at,title,mood,body,facts) VALUES (?,?,?,?,?,?)',
                                   (key, day, title, '平静', '这一天的几段对话写在一起。' + text, '[]'))
                    db.execute('INSERT INTO diary_comments VALUES (?,?,?)', (day, day, '合成评论'))
                else:
                    for table in ('diary_entries', 'diary_comments', 'diary_deleted'):
                        db.execute(f'DELETE FROM {table}')
            return web.json_response({'day': day})
        if name == 'reindex':
            return web.json_response(await reindex(server))
        return web.json_response({'error': 'unknown'}, status=404)

    return middleware


async def reindex(server) -> dict:
    """Run every indexer that can write a letter's text back: the outbox scan, the received-text index, vectors."""
    from runtime.memory import conversation_memory_runtime as cmr
    out = {}
    runtime = cmr._RUNTIME
    if runtime is not None:
        await runtime.outbox.scan_once()
        out['outbox'] = True
    adapter = server.conversation_memory_adapter
    with adapter._using_current() as current:
        rows = [dict(row) for row in server.store.letters]
        from runtime.memory.received_user_originals import index_received_rows
        index_received_rows(current, rows, user_id=server._memory_config.user_id)
        out['received'] = True
        out['vectors'] = int(current.index_original_vectors(server._memory_config.user_id, limit=64) or 0)
    return out


# --------------------------------------------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--jev-port', type=int, required=True)
    parser.add_argument('--mem0-site', default='')
    parser.add_argument('--script', default='')
    args = parser.parse_args()

    root = Path(args.root).resolve()
    data = root / 'data'
    data.mkdir(parents=True, exist_ok=True)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(CLIENT))
    if args.mem0_site:
        sys.path.append(args.mem0_site)
    os.environ.update({
        'PYTHONDONTWRITEBYTECODE': '1',
        'OLIVIA_LOCAL_DATA_ROOT': str(data),
        'OLIVIA_PROJECT_ROOT': str(root / 'app'),
        'OLIVIA_PROVIDER_CACHE_ROOT': str(data / 'provider-cache'),
        'OLIVIA_MEMORY_ENABLED': 'true',
        'OLIVIA_MEMORY_DEFAULT_PROVIDER': 'mem0',
        'OLIVIA_MEMORY_ROOT': str(data / 'memory'),
        'OLIVIA_MEMORY_EMBEDDING_CACHE': str(data / 'memory' / 'model-cache'),
        'OLIVIA_PRIVATE_WORLD_ENABLED': '1',
        'OLIVIA_PRIVATE_WORLD_CANDIDATES_ENABLED': '1',
        'OLIVIA_PRIVATE_WORLD_DB': str(data / 'private_world' / 'private_world.sqlite3'),
        'OLIVIA_REPLY_DELAY_ENABLED': '0',
        'OLIVIA_MEMORY_OUTBOX_INTERVAL_SECONDS': '0.5',
        # Mem0 needs an extraction model configured; the stand-in Jev service does the extraction and this is never called.
        'OLIVIA_MEMORY_LLM_BASE_URL': 'http://127.0.0.1:9/v1',
        'OLIVIA_MEMORY_LLM_MODEL': 'synthetic-extractor',
        'OLIVIA_PORT': str(args.port),
        'OLIVIA_LLM_PROVIDER': 'none',
        'OLIVIA_JEV_DECISION_URL': f'http://127.0.0.1:{args.jev_port}/v1/companion/decide',
        'HF_HUB_DISABLE_TELEMETRY': '1', 'MEM0_TELEMETRY': 'False', 'DO_NOT_TRACK': '1',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
    })
    os.environ.pop('OLIVIA_JEV_BILLING_ENABLED', None)
    block_external_network(root / 'network-blocked.log')
    start_jev(args.jev_port)
    patch_memory()
    from runtime.private_world import student_world

    async def no_weather(now):
        return None

    student_world.shanghai_weather = no_weather
    import local_server
    from aiohttp import web
    from original_client_server import create_configured_original_client_server_runtime
    script = Script(args.script)
    patch_stand_ins(local_server, script, root)
    local_server.recover_pending_private_world()
    runtime = create_configured_original_client_server_runtime(server_module=local_server)
    runtime.app.middlewares.insert(0, e2e_middleware(local_server, root, script))
    web.run_app(runtime.app, host='127.0.0.1', port=args.port, access_log=None, print=None)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
