"""Native MIDI upload/job adapter. Raw MIDI is validated, never rewritten locally."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import time
import uuid

from aiohttp import web
from runtime.cloud_service import CloudError
from runtime.remote_generation import RemoteGeneration
from runtime import piano

STATES = {'queued': 1, 'running': 2, 'succeeded': 3, 'cancelled': 4, 'failed': 5}
_locks: dict[str, asyncio.Lock] = {}


def api(server):
    return RemoteGeneration(server._os.environ.get('OLIVIA_GPU_API_URL', ''),
                            server._os.environ.get('OLIVIA_GPU_API_KEY', ''))


def directory(server):
    root = server._local_data_root()
    if root is None:
        raise CloudError('PIANO_STORAGE_UNAVAILABLE', 503)
    target = Path(root) / 'piano'
    target.mkdir(parents=True, exist_ok=True)
    return target


async def enabled(server):
    result = piano.capability(await api(server).request('capabilities', {}))
    if result is None:
        raise CloudError('PIANO_UNAVAILABLE', 503)
    return result


def upload_paths(server, token):
    if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
        raise ValueError('MIDI_UPLOAD_INVALID')
    root = directory(server)
    return root / (token + '.json'), root / (token + '.mid')


async def upload_http(server, request):
    """Single-use opaque upload URL; never fetch a caller-supplied URL/path."""
    headers = server.CORS_HEADERS(request)
    if request.method != 'PUT':
        return web.Response(status=405, headers=headers)
    try:
        caps = await enabled(server)
        metadata, target = upload_paths(server, request.path.rsplit('/', 1)[-1])
        record = json.loads(metadata.read_text(encoding='utf-8'))
        if time.time() - record['created_at'] > 3600 or target.exists():
            raise ValueError('MIDI_UPLOAD_EXPIRED')
        data = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > caps['max_midi_bytes']:
                raise ValueError('MIDI_TOO_LARGE')
        piano.validate_midi(record['filename'], bytes(data), caps['max_midi_bytes'])
        with target.open('xb') as stream:
            stream.write(data)
        return web.Response(status=204, headers=headers)
    except CloudError as exc:
        return web.json_response(server.err(exc.status, exc.code, {}), status=exc.status, headers=headers)
    except (ValueError, OSError, KeyError):
        return web.json_response(server.err(400, 'MIDI_UPLOAD_INVALID', {}), status=400, headers=headers)


def projection(server, job):
    value = {'jobId': job['job_id'], 'state': job['state'], 'status': job['status'],
             'filename': job.get('filename', ''), 'createdAt': job.get('created_at', 0)}
    if job.get('piano_meta'):
        value['piano_meta'] = job['piano_meta']
        value['description'] = piano.completion_text(job['piano_meta'])
    if job['state'] == 3:
        url = f"http://127.0.0.1:{server.PORT}/toy/midi/audio/{job['job_id']}.mp3"
        # The shipped video.js preview hardcodes video/mp4. Our native-card
        # enhancement consumes audioUrl, so never mislabel MP3 as video.
        value['info'] = {'videoUrls': [], 'audioUrl': url}
    return value


async def refresh(server, job):
    async with _locks.setdefault(job['job_id'], asyncio.Lock()):
        if job['state'] in {4, 5} or job['state'] == 3 and job.get('remembered'):
            return
        remote = api(server)
        if not job.get('task_id'):
            submitted = await remote.request('submit', {'request_id': job['job_id'], 'kind': 'piano', 'input': job['input']})
            job['task_id'] = submitted['task_id']
            server._persist_store_state()
        result = await remote.request('status', {'task_id': job['task_id']})
        status = result['status']
        if status == 'succeeded':
            meta = piano.validate_meta(result.get('piano_meta'))
            output = directory(server) / (job['job_id'] + '.mp3')
            if not output.is_file():
                from runtime.personal_chat.piano import validate_audio
                await remote.download_task(job['task_id'], output, validate=validate_audio)
            job['piano_meta'] = meta
            await piano.remember(server, job['job_id'], meta, channel='client')
            job['remembered'] = True
        job.update(status=status, state=STATES[status])
        server._persist_store_state()


def monitor(server, job):
    """Finish recording/memory even after the user closes the native panel."""
    tasks = getattr(server, '_native_piano_tasks', None)
    if tasks is None:
        tasks = server._native_piano_tasks = {}
    previous = tasks.get(job['job_id'])
    if previous is not None and not previous.done():
        return
    async def run():
        for _ in range(450):
            try:
                await refresh(server, job)
            except (CloudError, ValueError, OSError):
                pass  # A later status read resumes the same paid task.
            if job['state'] in {3, 4, 5}:
                break
            await asyncio.sleep(4)
    tasks[job['job_id']] = asyncio.create_task(run())


async def audio_http(server, request):
    if request.method not in {'GET', 'HEAD'}:
        return web.Response(status=405)
    job_id = request.path.rsplit('/', 1)[-1].removesuffix('.mp3')
    job = next((j for j in server.store.midi_jobs if j.get('job_id') == job_id and j.get('state') == 3), None)
    if job is None or not re.fullmatch(r'[a-f0-9]{32}', job_id):
        return web.Response(status=404)
    path = directory(server) / (job_id + '.mp3')
    if not path.is_file():
        return web.Response(status=404)
    headers = {'Content-Type': 'audio/mpeg', 'Cache-Control': 'private, no-cache', **server.CORS_HEADERS(request)}
    if request.query.get('download') == '1':
        headers['Content-Disposition'] = 'attachment; filename="piano.mp3"'
    return web.FileResponse(path, headers=headers)


async def route(server, path, body, query):
    try:
        if path in {'/toy/midi/getGenerateResult', '/toy/midi/cancelGenerate', '/toy/midi/deleteJob'}:
            identity = body.get('jobId') or query.get('jobId') or query.get('job_id')
            if not any(j.get('job_id') == identity and j.get('task_id') and not j.get('hidden') for j in server.store.midi_jobs):
                return server.err(404, 'MIDI_JOB_NOT_FOUND', {})
        caps = await enabled(server)
        if path == '/toy/midi/capabilities':
            return server.ok(caps)
        if path == '/toy/genObjectUploadUrl':
            filename = body.get('filename', '')
            if not isinstance(filename, str) or len(filename) > 255 or Path(filename).suffix.lower() not in {'.mid', '.midi'}:
                raise ValueError('MIDI_FILENAME_INVALID')
            token = uuid.uuid4().hex
            metadata, _ = upload_paths(server, token)
            metadata.write_text(json.dumps({'filename': filename, 'created_at': time.time()}), encoding='utf-8')
            return server.ok({'url': f'http://127.0.0.1:{server.PORT}/toy/midi/upload/{token}', 'key': token, 'headers': {}})
        if path == '/toy/midi/generate':
            token = body.get('midiUrl', '')
            async with _locks.setdefault('upload:' + str(token), asyncio.Lock()):
                metadata, source = upload_paths(server, token)
                record = json.loads(metadata.read_text(encoding='utf-8'))
                piano.validate_midi(record['filename'], source.read_bytes(), caps['max_midi_bytes'])
                title = body.get('title') or Path(record['filename']).stem[:60]
                seconds = body.get('seconds', min(60, caps['max_seconds']))
                payload = piano.validate_input({'source': 'midi', 'midi_asset': '0' * 64, 'title': title, 'seconds': seconds})
                if seconds > caps['max_seconds']:
                    raise ValueError('PIANO_SECONDS_INVALID')
                previous = next((j for j in server.store.midi_jobs if j.get('upload_token') == token), None)
                if previous is not None and previous.get('task_id'):
                    return server.ok(projection(server, previous))
                remote = api(server)
                payload['midi_asset'] = await remote.upload(source)
                if previous is None:
                    previous = {'job_id': uuid.uuid4().hex, 'upload_token': token, 'filename': record['filename'],
                                'created_at': int(time.time()), 'status': 'queued', 'state': 1, 'input': payload}
                    server.store.midi_jobs.append(previous)
                    server._persist_store_state()
                elif previous.get('input') != payload:
                    return server.err(409, 'PIANO_SUBMISSION_ALREADY_STARTED', {})
                try:
                    result = await remote.request('submit', {'request_id': previous['job_id'], 'kind': 'piano', 'input': previous['input']})
                except CloudError as exc:
                    # Resume only uncertain transport failures. A definitive
                    # rejection must not turn into a later paid background job.
                    if exc.code in {'GPU_CONNECTION_TIMEOUT', 'GPU_CONNECT_FAILED', 'GPU_CONNECTION_FAILED'}:
                        monitor(server, previous)
                    else:
                        previous.update(state=5, status='failed')
                        server._persist_store_state()
                    raise
                previous.update(task_id=result['task_id'], state=STATES[result['status']], status=result['status'])
                # Completion is published only after the local MP3 and memory exist.
                if previous['state'] == 3:
                    previous.update(state=2, status='running')
                server._persist_store_state()
                monitor(server, previous)
                return server.ok(projection(server, previous))
        jobs = [j for j in server.store.midi_jobs if (j.get('task_id') or j.get('input')) and not j.get('hidden')]
        if path == '/toy/midi/listJobs':
            cursor, size = max(0, int(query.get('cursor', 0))), max(1, min(100, int(query.get('pageSize', 20))))
            selected = list(reversed(jobs))[cursor:cursor + size]
            for job in selected:
                await refresh(server, job)
            return server.ok({'total': len(jobs), 'list': [projection(server, j) for j in selected],
                              'nextCursor': cursor + len(selected), 'hasMore': cursor + len(selected) < len(jobs)})
        if path == '/toy/midi/batchGetResult':
            ids = []
            for key in ('jobIds', 'jobIds[]', 'job_ids'):
                value = query.getall(key, []) if hasattr(query, 'getall') else query.get(key, [])
                ids.extend([value] if isinstance(value, str) else value)
            selected = [j for j in jobs if not ids or j['job_id'] in ids][:100]
            for job in selected:
                await refresh(server, job)
            return server.ok({'results': [projection(server, j) for j in selected], 'generatedToday': len(jobs),
                              'dailyLimit': caps.get('daily_limit', 0)})
        job_id = body.get('jobId') or query.get('jobId') or query.get('job_id')
        job = next((j for j in jobs if j['job_id'] == job_id), None)
        if job is None:
            return server.err(404, 'MIDI_JOB_NOT_FOUND', {})
        if path == '/toy/midi/getGenerateResult':
            await refresh(server, job)
        elif path in {'/toy/midi/cancelGenerate', '/toy/midi/deleteJob'}:
            async with _locks.setdefault(job_id, asyncio.Lock()):
                if job['state'] in {1, 2}:
                    result = await api(server).request('cancel', {'task_id': job['task_id']})
                    if result['status'] != 'succeeded':
                        job.update(status=result['status'], state=STATES[result['status']])
                if path.endswith('/deleteJob'):
                    job['hidden'] = True
                server._persist_store_state()
            if job['state'] in {1, 2}:
                await refresh(server, job)
        return server.ok(projection(server, job))
    except CloudError as exc:
        if path == '/toy/midi/capabilities':
            return server.ok({'enabled': False})
        return server.err(exc.status, exc.code, {})
    except (ValueError, OSError, KeyError, TypeError):
        return server.err(400, 'MIDI_REQUEST_INVALID', {})
