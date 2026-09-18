"""Durable task API for one GPU host; bind loopback behind HTTPS."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlsplit
from contextlib import contextmanager
from aiohttp import web

ID = re.compile(r'[A-Za-z0-9_-]{1,100}')
TERMINAL = {'succeeded', 'failed', 'cancelled'}
SCHEMAS = {
    'tts': ({'text', 'voice_plan'}, {'text'}),
    'cover': ({'source_asset', 'lyrics', 'language'}, {'source_asset'}),
    'original': ({'lyrics'}, {'lyrics'}),
    'video': ({'text', 'voice_plan', 'scene_asset', 'adaptive_delivery', 'enforce_content_gate'}, {'text', 'scene_asset'}),
    'lipsync': ({'audio_asset', 'scene_asset'}, {'audio_asset', 'scene_asset'}),
    'separate': ({'audio_asset'}, {'audio_asset'}),
}


def kill_posix_tree(pid):
    # Model runners create their own sessions, so killing only the API worker's
    # process group leaves those GPU processes alive.
    import psutil
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
    except psutil.NoSuchProcess:
        pass


def _positive_number(value, default):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default


def build_app(config, root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ('jobs', 'uploads'):
        (root / name).mkdir(exist_ok=True)

    @contextmanager
    def db():
        connection = sqlite3.connect(root / 'tasks.sqlite3', timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=15000')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with db() as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
          digest TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
          status TEXT NOT NULL, created REAL NOT NULL, elapsed REAL,
          stage TEXT NOT NULL DEFAULT 'queued', started REAL, heartbeat REAL,
          gpu_id TEXT, completed REAL, gpu_elapsed REAL, upload_elapsed REAL,
          UNIQUE(owner, request_id));
        CREATE TABLE IF NOT EXISTS assets(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, path TEXT NOT NULL);
        ''')
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(tasks)')}
        additions = {
            'stage': "TEXT NOT NULL DEFAULT 'queued'",
            'started': 'REAL',
            'heartbeat': 'REAL',
            'gpu_id': 'TEXT',
            'completed': 'REAL',
            'gpu_elapsed': 'REAL',
            'upload_elapsed': 'REAL',
        }
        added_stage = False
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f'ALTER TABLE tasks ADD COLUMN {name} {declaration}')
                added_stage = added_stage or name == 'stage'
        if added_stage:
            connection.execute("""UPDATE tasks SET stage=CASE
                WHEN status='queued' THEN 'queued'
                WHEN status='running' THEN 'gpu_running'
                WHEN status='cancelled' THEN 'cancelled'
                WHEN status='failed' THEN 'failed'
                WHEN status='succeeded' THEN 'completed'
                ELSE 'failed' END""")
        connection.executescript('''
        CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created);
        CREATE INDEX IF NOT EXISTS idx_tasks_owner_status ON tasks(owner, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_stage_status ON tasks(stage, status);
        ''')

    public = config['public_url'].rstrip('/')
    parsed = urlsplit(public)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path or not (parsed.scheme == 'https'
                                   or (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'))):
        raise ValueError('HTTPS required')
    signing = config['signing_key'].encode()
    if len(signing) < 32 or not config['tokens']:
        raise ValueError('Secrets required')

    wake = asyncio.Event()
    upload_queue = asyncio.Queue()
    processes = {}
    upload_active = set()
    upload_queued = set()
    upload_workers_count = max(1, int(_positive_number(config.get('result_upload_workers'), 2)))
    profiles = config.get('profiles', {})
    gpu_ids = [str(gpu) for gpu in config.get('gpus', ['0'])]
    worker_state = {
        gpu: {'alive': False, 'task_id': '', 'restarts': 0, 'last_error': ''}
        for gpu in gpu_ids
    }
    store = None
    if config.get('result_store'):
        from gpu_service.result_store import ResultStore
        store = ResultStore(config['result_store'])
    if set(profiles) - SCHEMAS.keys():
        raise ValueError('Unsupported profile')

    queue_sla = config.get('queue_sla_seconds', {})
    task_timeouts = config.get('task_timeouts', {})
    heartbeat_interval = _positive_number(config.get('heartbeat_interval_seconds'), 5)
    restart_delay = _positive_number(config.get('worker_restart_delay_seconds'), 1)

    def sla_for(kind):
        if isinstance(queue_sla, dict):
            default = _positive_number(queue_sla.get('default'), 1800)
            return _positive_number(queue_sla.get(kind), 600 if kind == 'tts' else default)
        return 600 if kind == 'tts' else 1800

    def timeout_for(kind):
        if isinstance(task_timeouts, dict):
            default = _positive_number(task_timeouts.get('default'), config.get('task_timeout_seconds', 3600))
            return _positive_number(task_timeouts.get(kind), default)
        return _positive_number(config.get('task_timeout_seconds'), 3600)

    def owner(request):
        value = request.headers.get('Authorization', '')
        for name, token in config['tokens'].items():
            if len(token) >= 32 and hmac.compare_digest(value, 'Bearer ' + token):
                return name
        raise web.HTTPUnauthorized()

    def asset(owner_id, aid):
        if not isinstance(aid, str) or not ID.fullmatch(aid):
            raise web.HTTPBadRequest(text='ASSET_INVALID')
        shared = config.get('shared_assets', {}).get(aid)
        if shared:
            return str(Path(shared['path']).resolve())
        with db() as connection:
            row = connection.execute('SELECT path FROM assets WHERE id=? AND owner=?', (aid, owner_id)).fetchone()
        if not row:
            raise web.HTTPNotFound(text='ASSET_NOT_FOUND')
        return row['path']

    def queue_position(connection, row):
        if row['status'] != 'queued':
            return None
        queued = connection.execute("SELECT id, kind, created FROM tasks WHERE status='queued'").fetchall()
        ordered = sorted(queued, key=lambda item: (
            item['created'] + sla_for(item['kind']), item['created'], item['id']))
        for index, item in enumerate(ordered, start=1):
            if item['id'] == row['id']:
                return index
        return None

    def projection(row, connection=None):
        outputs = []
        if row['status'] == 'succeeded':
            expires = int(time.time()) + 900
            sig = hmac.new(signing, f"{row['id']}:{expires}".encode(), hashlib.sha256).hexdigest()
            outputs = [{'url': f"{public}/v1/results/{row['id']}?expires={expires}&signature={sig}"}]
            if store and (root / 'jobs' / row['id'] / 'r2-result.json').is_file():
                outputs = [{'url': store.url(root / 'jobs' / row['id'])}]
        now = time.time()
        if row['started'] is not None:
            queue_end = row['started']
        elif row['status'] == 'queued':
            queue_end = now
        else:
            queue_end = row['created']
        queued_seconds = max(0.0, queue_end - row['created'])
        sla = sla_for(row['kind'])
        result = {
            'task_id': row['id'],
            'status': row['status'],
            'stage': row['stage'],
            'outputs': outputs,
            'queued_seconds': round(queued_seconds, 3),
            'queue_sla_seconds': sla,
            'sla_exceeded': queued_seconds > sla,
        }
        if row['gpu_id']:
            result['gpu_id'] = row['gpu_id']
        if row['gpu_elapsed'] is not None:
            result['gpu_elapsed_seconds'] = round(row['gpu_elapsed'], 3)
        if row['upload_elapsed'] is not None:
            result['upload_elapsed_seconds'] = round(row['upload_elapsed'], 3)
        if connection is not None and row['status'] == 'queued':
            result['queue_position'] = queue_position(connection, row)
        return result

    async def capabilities(request):
        owner(request)
        return web.json_response({
            'kinds': list(profiles),
            'shared_assets': [
                {'asset_id': key, 'sha256': value['sha256']}
                for key, value in config.get('shared_assets', {}).items()
            ],
            'billing_enabled': False,
            'max_upload_bytes': config.get('max_upload_bytes', 268435456),
            'queue_sla_seconds': {'tts': sla_for('tts'), 'default': sla_for('video')},
        })

    async def health(request):
        owner(request)
        now = time.time()
        with db() as connection:
            rows = connection.execute(
                "SELECT status, stage, kind, created FROM tasks WHERE status IN ('queued','running')"
            ).fetchall()
        queued = [row for row in rows if row['status'] == 'queued']
        running = [row for row in rows if row['status'] == 'running']
        alive = sum(1 for state in worker_state.values() if state['alive'])
        breached = sum(1 for row in queued if now - row['created'] > sla_for(row['kind']))
        payload = {
            'status': 'OK' if alive == len(gpu_ids) else 'DEGRADED',
            'workers_alive': alive,
            'workers_configured': len(gpu_ids),
            'worker_restarts': sum(state['restarts'] for state in worker_state.values()),
            'queue_depth': len(queued),
            'oldest_queue_seconds': round(max((now - row['created'] for row in queued), default=0.0), 3),
            'queue_sla_exceeded': breached,
            'gpu_running': sum(1 for row in running if row['stage'] == 'gpu_running'),
            'uploads_running': len(upload_active),
            'uploads_queued': upload_queue.qsize(),
            'workers': [
                {'gpu_id': gpu, 'alive': state['alive'], 'busy': bool(state['task_id']),
                 'restarts': state['restarts'], 'last_error': state['last_error']}
                for gpu, state in worker_state.items()
            ],
        }
        return web.json_response(payload)

    upload_locks = {name: asyncio.Lock() for name in config['tokens']}

    async def upload(request):
        who = owner(request)
        async with upload_locks[who]:
            return await store_upload(request, who)

    async def store_upload(request, who):
        suffix = request.headers.get('X-Asset-Suffix', '')
        if suffix not in ('.wav', '.mp3', '.flac', '.mp4', '.png', '.jpg'):
            raise web.HTTPBadRequest()
        limit = config.get('max_upload_bytes', 268435456)
        with db() as connection:
            paths = [row[0] for row in connection.execute('SELECT path FROM assets WHERE owner=?', (who,))]
        remaining = config.get('upload_quota_bytes', 2147483648) - sum(
            Path(path).stat().st_size for path in paths if Path(path).exists())
        if len(paths) >= 100 or remaining <= 0:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=limit + 1)
        limit = min(limit, remaining)
        aid = secrets.token_hex(16)
        path = root / 'uploads' / (aid + suffix)
        size = 0
        try:
            with path.open('xb') as target:
                async for chunk in request.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > limit:
                        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=size)
                    target.write(chunk)
            if not size:
                raise web.HTTPBadRequest()
            with db() as connection:
                connection.execute('INSERT INTO assets VALUES(?,?,?)', (aid, who, str(path)))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return web.json_response({'asset_id': aid}, status=201)

    async def submit(request):
        who = owner(request)
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest()
        if not isinstance(body, dict) or set(body) != {'request_id', 'kind', 'input'}:
            raise web.HTTPBadRequest()
        rid, kind, data = body['request_id'], body['kind'], body['input']
        if (not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', rid)
                or request.headers.get('Idempotency-Key') != rid):
            raise web.HTTPBadRequest()
        if not isinstance(kind, str) or kind not in profiles:
            raise web.HTTPBadRequest(text='CAPABILITY_UNAVAILABLE')
        allowed, required = SCHEMAS[kind]
        if not isinstance(data, dict) or set(data) - allowed or required - set(data):
            raise web.HTTPBadRequest()
        for key, value in data.items():
            if key in ('adaptive_delivery', 'enforce_content_gate'):
                if not isinstance(value, bool):
                    raise web.HTTPBadRequest()
            elif key == 'voice_plan':
                if not isinstance(value, dict):
                    raise web.HTTPBadRequest()
            elif not isinstance(value, str) or len(value) > 16000:
                raise web.HTTPBadRequest()
            if key.endswith('_asset'):
                asset(who, value)
        try:
            canonical = json.dumps(body, sort_keys=True, allow_nan=False)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest()
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT * FROM tasks WHERE owner=? AND request_id=?', (who, rid)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise web.HTTPConflict(text='IDEMPOTENCY_CONFLICT')
                return web.json_response(projection(row, connection))
            active_limit = int(_positive_number(config.get('max_active_tasks_per_owner'), 5))
            if connection.execute(
                "SELECT count(*) FROM tasks WHERE owner=? AND status IN ('queued','running')", (who,)
            ).fetchone()[0] >= active_limit:
                raise web.HTTPTooManyRequests()
            global_limit = int(_positive_number(config.get('max_active_tasks'), 100))
            if connection.execute(
                "SELECT count(*) FROM tasks WHERE status IN ('queued','running')"
            ).fetchone()[0] >= global_limit:
                raise web.HTTPTooManyRequests()
            tid = secrets.token_hex(16)
            connection.execute(
                '''INSERT INTO tasks(id,owner,request_id,digest,kind,payload,status,created,elapsed,stage)
                   VALUES(?,?,?,?,?,?,?,?,NULL,'queued')''',
                (tid, who, rid, digest, kind, json.dumps(data), 'queued', time.time()))
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            response = projection(row, connection)
        wake.set()
        return web.json_response(response, status=202)

    async def task(request):
        who = owner(request)
        tid = request.match_info['tid']
        with db() as connection:
            row = connection.execute('SELECT * FROM tasks WHERE id=? AND owner=?', (tid, who)).fetchone()
            if not row:
                raise web.HTTPNotFound()
            if request.method == 'POST' and row['status'] in ('queued', 'running'):
                connection.execute(
                    """UPDATE tasks SET status='cancelled',stage='cancelled',completed=?,heartbeat=?
                       WHERE id=? AND status IN ('queued','running')""",
                    (time.time(), time.time(), tid))
                row = connection.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            response = projection(row, connection)
        if request.method == 'POST':
            proc = processes.get(tid)
            if proc:
                await stop(proc)
        return web.json_response(response)

    async def result(request):
        tid = request.match_info['tid']
        try:
            expires = int(request.query['expires'])
        except (KeyError, ValueError):
            raise web.HTTPForbidden()
        expected = hmac.new(signing, f'{tid}:{expires}'.encode(), hashlib.sha256).hexdigest()
        if (not ID.fullmatch(tid) or expires < time.time()
                or not hmac.compare_digest(expected, request.query.get('signature', ''))):
            raise web.HTTPForbidden()
        with db() as connection:
            row = connection.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()
        if not row or row['status'] != 'succeeded':
            raise web.HTTPNotFound()
        return web.FileResponse(root / 'jobs' / tid / 'output.bin', headers={
            'Cache-Control': 'private, no-store', 'Content-Disposition': 'attachment'})

    async def stop(proc):
        if proc.returncode is not None:
            return
        if os.name == 'nt':
            killer = await asyncio.create_subprocess_exec(
                'taskkill', '/PID', str(proc.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=0x08000000)
            await killer.wait()
        else:
            kill_posix_tree(proc.pid)
        await proc.wait()

    def claim(gpu):
        now = time.time()
        with db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY created LIMIT 200").fetchall()
            if not rows:
                return None
            row = min(rows, key=lambda item: (
                item['created'] + sla_for(item['kind']), item['created'], item['id']))
            changed = connection.execute(
                """UPDATE tasks SET status='running',stage='gpu_running',started=?,heartbeat=?,gpu_id=?
                   WHERE id=? AND status='queued'""",
                (now, now, str(gpu), row['id'])).rowcount
            if not changed:
                return None
            return connection.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone()

    async def wait_process(proc, task_id, timeout):
        deadline = time.monotonic() + timeout
        wait_task = asyncio.create_task(proc.wait())
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                done, _ = await asyncio.wait({wait_task}, timeout=min(heartbeat_interval, remaining))
                if done:
                    return wait_task.result()
                with db() as connection:
                    connection.execute(
                        "UPDATE tasks SET heartbeat=? WHERE id=? AND status='running' AND stage='gpu_running'",
                        (time.time(), task_id))
        finally:
            if not wait_task.done():
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)

    async def execute(row, gpu):
        job = root / 'jobs' / row['id']
        job.mkdir(exist_ok=True)
        started = time.monotonic()
        proc = None
        worker_state[str(gpu)]['task_id'] = row['id']
        try:
            data = json.loads(row['payload'])
            for key in list(data):
                if key.endswith('_asset'):
                    data[key] = asset(row['owner'], data[key])
            spec = {
                'kind': row['kind'], 'input': data, 'profile': profiles[row['kind']],
                'output': str(job / 'output.bin'), 'job': str(job)}
            (job / 'request.json').write_text(json.dumps(spec), encoding='utf-8')
            env = {
                **os.environ,
                'CUDA_VISIBLE_DEVICES': str(gpu),
                'OLIVIA_GPU_ROUTE': 'local',
                'PYTHONIOENCODING': 'utf-8',
            }
            kwargs = {'creationflags': 0x08000000} if os.name == 'nt' else {'start_new_session': True}
            with (job / 'worker.log').open('wb') as log:
                proc = await asyncio.create_subprocess_exec(
                    config.get('python', sys.executable), '-m', 'gpu_service.worker', str(job / 'request.json'),
                    cwd=Path(__file__).resolve().parents[1], env=env, stdout=log, stderr=log, **kwargs)
                processes[row['id']] = proc
                with db() as connection:
                    state = connection.execute('SELECT status FROM tasks WHERE id=?', (row['id'],)).fetchone()[0]
                if state == 'cancelled':
                    await stop(proc)
                else:
                    await wait_process(proc, row['id'], timeout_for(row['kind']))
            gpu_elapsed = time.monotonic() - started
            success = (proc.returncode == 0 and (job / 'output.bin').is_file()
                       and (job / 'output.bin').stat().st_size > 0)
            with db() as connection:
                state = connection.execute('SELECT status FROM tasks WHERE id=?', (row['id'],)).fetchone()[0]
                if state == 'cancelled':
                    return
                if not success:
                    connection.execute(
                        """UPDATE tasks SET status='failed',stage='failed',gpu_elapsed=?,elapsed=?,completed=?,heartbeat=?
                           WHERE id=? AND status='running'""",
                        (gpu_elapsed, time.time() - row['created'], time.time(), time.time(), row['id']))
                    return
                if store:
                    connection.execute(
                        """UPDATE tasks SET status='succeeded',stage='uploading',gpu_elapsed=?,elapsed=?,completed=?,heartbeat=?
                           WHERE id=? AND status='running'""",
                        (gpu_elapsed, time.time() - row['created'], time.time(), time.time(), row['id']))
                else:
                    connection.execute(
                        """UPDATE tasks SET status='succeeded',stage='completed',gpu_elapsed=?,elapsed=?,completed=?,heartbeat=?
                           WHERE id=? AND status='running'""",
                        (gpu_elapsed, time.time() - row['created'], time.time(), time.time(), row['id']))
            if store:
                if row['id'] not in upload_queued and row['id'] not in upload_active:
                    upload_queued.add(row['id'])
                    upload_queue.put_nowait(row['id'])
        except asyncio.CancelledError:
            if proc:
                await stop(proc)
            raise
        except Exception:
            if proc:
                await stop(proc)
            logging.exception('GPU_TASK_FAILED task=%s gpu=%s', row['id'], gpu)
            with db() as connection:
                connection.execute(
                    """UPDATE tasks SET status='failed',stage='failed',gpu_elapsed=?,elapsed=?,completed=?,heartbeat=?
                       WHERE id=? AND status='running'""",
                    (time.monotonic() - started, time.time() - row['created'], time.time(), time.time(), row['id']))
        finally:
            processes.pop(row['id'], None)
            worker_state[str(gpu)]['task_id'] = ''

    async def work(gpu):
        state = worker_state[str(gpu)]
        state['alive'] = True
        state['last_error'] = ''
        try:
            while True:
                try:
                    row = claim(gpu)
                except Exception as exc:
                    state['last_error'] = type(exc).__name__
                    logging.exception('GPU_QUEUE_CLAIM_FAILED gpu=%s', gpu)
                    await asyncio.sleep(1)
                    continue
                state['last_error'] = ''
                if not row:
                    try:
                        await asyncio.wait_for(wake.wait(), 2)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        wake.clear()
                    continue
                await execute(row, gpu)
        finally:
            state['alive'] = False
            state['task_id'] = ''

    async def supervise(gpu):
        state = worker_state[str(gpu)]
        while True:
            task_worker = asyncio.create_task(work(gpu), name=f'gpu-worker-{gpu}')
            try:
                await task_worker
                state['last_error'] = 'WORKER_EXITED'
            except asyncio.CancelledError:
                task_worker.cancel()
                await asyncio.gather(task_worker, return_exceptions=True)
                raise
            except Exception as exc:
                state['last_error'] = type(exc).__name__
                logging.exception('GPU_WORKER_CRASHED gpu=%s', gpu)
            state['restarts'] += 1
            await asyncio.sleep(restart_delay)

    async def upload_worker(index):
        while True:
            task_id = await upload_queue.get()
            upload_queued.discard(task_id)
            upload_active.add(task_id)
            started = time.monotonic()
            try:
                with db() as connection:
                    row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
                if not row or row['status'] != 'succeeded' or row['stage'] != 'uploading':
                    continue
                try:
                    await asyncio.to_thread(store.publish, task_id, row['kind'], root / 'jobs' / task_id)
                except Exception:
                    # Generation is already complete. Keep the direct-download fallback and
                    # never force the GPU to regenerate because object storage is degraded.
                    logging.warning('RESULT_STORE_UPLOAD_FAILED task=%s', task_id, exc_info=True)
                upload_elapsed = time.monotonic() - started
                with db() as connection:
                    connection.execute(
                        """UPDATE tasks SET stage='completed',upload_elapsed=?,heartbeat=?
                           WHERE id=? AND status='succeeded' AND stage='uploading'""",
                        (upload_elapsed, time.time(), task_id))
            finally:
                upload_active.discard(task_id)
                upload_queue.task_done()

    async def lifecycle(app):
        # Only GPU-execution interruptions are terminal. Completed generation waiting
        # for object storage is durable and resumes without charging another GPU run.
        with db() as connection:
            connection.execute(
                """UPDATE tasks SET status='failed',stage='failed',completed=?,heartbeat=?
                   WHERE status='running' AND stage!='uploading'""", (time.time(), time.time()))
            uploading = connection.execute(
                "SELECT id FROM tasks WHERE status='succeeded' AND stage='uploading'").fetchall()
            if not store:
                connection.execute(
                    """UPDATE tasks SET stage='completed',heartbeat=?
                       WHERE status='succeeded' AND stage='uploading'""", (time.time(),))
                uploading = []
            for row in uploading:
                output = root / 'jobs' / row['id'] / 'output.bin'
                if output.is_file() and output.stat().st_size > 0:
                    upload_queued.add(row['id'])
                    upload_queue.put_nowait(row['id'])
                else:
                    connection.execute(
                        """UPDATE tasks SET status='failed',stage='failed',completed=?,heartbeat=?
                           WHERE id=? AND status='succeeded' AND stage='uploading'""",
                        (time.time(), time.time(), row['id']))
        for shared in config.get('shared_assets', {}).values():
            with Path(shared['path']).open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != shared['sha256']:
                raise ValueError('Shared asset hash mismatch')
        upload_workers = [
            asyncio.create_task(upload_worker(index), name=f'result-upload-{index}')
            for index in range(upload_workers_count)
        ] if store else []
        supervisors = [
            asyncio.create_task(supervise(gpu), name=f'gpu-supervisor-{gpu}')
            for gpu in gpu_ids
        ]
        yield
        for task_worker in supervisors + upload_workers:
            task_worker.cancel()
        await asyncio.gather(*(supervisors + upload_workers), return_exceptions=True)

    app = web.Application(client_max_size=32768)
    app.router.add_get('/v1/capabilities', capabilities)
    app.router.add_get('/v1/health', health)
    app.router.add_post('/v1/assets', upload)
    app.router.add_post('/v1/tasks', submit)
    app.router.add_get('/v1/tasks/{tid}', task)
    app.router.add_post('/v1/tasks/{tid}/cancel', task)
    app.router.add_get('/v1/results/{tid}', result)
    app.cleanup_ctx.append(lifecycle)
    return app


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18880)
    args = parser.parse_args()
    web.run_app(build_app(json.loads(args.config.read_text(encoding='utf-8')), args.data),
                host='127.0.0.1', port=args.port, access_log=None)
