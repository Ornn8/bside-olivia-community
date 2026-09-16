"""Optional independent GPU task protocol; no account-service cookies or retries."""
import json
import re
import asyncio
import hashlib
from pathlib import Path
import tempfile
import time
import uuid
from urllib.parse import urlsplit
from aiohttp import ClientSession, ClientTimeout, ClientError, ClientSSLError, ClientConnectorError
from runtime.cloud_service import endpoint, CloudError


def connection_error(exc):
    if isinstance(exc, ClientSSLError): return CloudError('GPU_TLS_FAILED')
    if isinstance(exc, TimeoutError): return CloudError('GPU_CONNECTION_TIMEOUT')
    if isinstance(exc, ClientConnectorError): return CloudError('GPU_CONNECT_FAILED')
    return CloudError('GPU_CONNECTION_FAILED')


class RemoteGeneration:
    def __init__(self, url='', token=''):
        self.url = endpoint(url) if url else ''
        self.token = token

    async def request(self, action, data):
        if not self.url or not self.token:
            raise CloudError('GPU_NOT_CONFIGURED', 503)
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}
        payload = None
        if action == 'submit':
            if set(data) != {'request_id', 'kind', 'input'} or data['kind'] not in ('tts', 'cover', 'image', 'video', 'original', 'lipsync', 'separate') or not isinstance(data['input'], dict):
                raise CloudError('GPU_REQUEST_INVALID', 400)
            if not isinstance(data['request_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', data['request_id']):
                raise CloudError('GPU_REQUEST_INVALID', 400)
            payload = data
            headers['Idempotency-Key'] = data['request_id']
            path, method = '/v1/tasks', 'POST'
        elif action == 'capabilities' and data == {}:
            path, method = '/v1/capabilities', 'GET'
        elif action in ('status', 'cancel'):
            if set(data) != {'task_id'} or not isinstance(data['task_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', data['task_id']):
                raise CloudError('GPU_REQUEST_INVALID', 400)
            path = '/v1/tasks/' + data['task_id'] + ('/cancel' if action == 'cancel' else '')
            method = 'POST' if action == 'cancel' else 'GET'
            if action == 'cancel': payload = {}
        else:
            raise CloudError('GPU_REQUEST_INVALID', 400)
        try:
            encoded = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        except (ValueError, TypeError):
            raise CloudError('GPU_REQUEST_INVALID', 400) from None
        if encoded is not None and len(encoded) > 32768:
            raise CloudError('GPU_REQUEST_TOO_LARGE', 413)
        headers['Content-Type'] = 'application/json'
        try:
            async with ClientSession(timeout=ClientTimeout(total=20), trust_env=False) as session:
                async with session.request(method, self.url + path, data=encoded, headers=headers, allow_redirects=False) as response:
                    if response.status in (401, 403): raise CloudError('GPU_AUTH_FAILED', 502)
                    if response.status == 429: raise CloudError('GPU_QUEUE_FULL', 429)
                    if response.status not in (200, 201, 202):
                        raise CloudError('GPU_REQUEST_FAILED', 502)
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        raw.extend(chunk)
                        if len(raw) > 262144: raise CloudError('GPU_RESPONSE_INVALID', 502)
                    result = json.loads(raw)
            if action == 'capabilities':
                if not isinstance(result, dict) or not isinstance(result.get('kinds'), list) or not isinstance(result.get('shared_assets'), list): raise ValueError()
                for item in result['shared_assets']:
                    if not isinstance(item, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', item.get('asset_id', '')) or not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '')): raise ValueError()
                return result
            if not isinstance(result, dict) or not isinstance(result.get('task_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', result['task_id']):
                raise ValueError()
            if action != 'submit' and result['task_id'] != data['task_id']: raise ValueError()
            if result.get('status') not in ('queued', 'running', 'succeeded', 'failed', 'cancelled'): raise ValueError()
            outputs = result.get('outputs', [])
            if not isinstance(outputs, list) or len(outputs) > 8: raise ValueError()
            cleaned = []
            for item in outputs:
                if not isinstance(item, dict) or not isinstance(item.get('url'), str): raise ValueError()
                url = urlsplit(item['url'])
                loopback_result = (self.url.startswith('http://') and url.scheme == 'http'
                                   and url.netloc == urlsplit(self.url).netloc)
                if (url.scheme != 'https' and not loopback_result) or not url.hostname or url.username or url.password: raise ValueError()
                cleaned.append({'url': item['url']})
            return {'task_id': result['task_id'], 'status': result['status'], 'outputs': cleaned}
        except (ClientError, TimeoutError) as exc:
            raise connection_error(exc) from None
        except (ValueError, TypeError, UnicodeError):
            raise CloudError('GPU_RESPONSE_INVALID', 502) from None

    async def upload(self, path):
        path = Path(path)
        if path.suffix.lower() not in ('.wav', '.mp3', '.flac', '.mp4', '.png', '.jpg') or not 0 < path.stat().st_size <= 268435456:
            raise CloudError('GPU_ASSET_INVALID', 400)
        if not self.url or not self.token: raise CloudError('GPU_NOT_CONFIGURED', 503)
        try:
            async with ClientSession(timeout=ClientTimeout(total=600), trust_env=False) as session:
                with path.open('rb') as source:
                    async with session.post(self.url + '/v1/assets', data=source,
                        headers={'Authorization': 'Bearer ' + self.token, 'X-Asset-Suffix': path.suffix.lower()},
                        allow_redirects=False) as response:
                        if response.status != 201: raise CloudError('GPU_UPLOAD_FAILED', 502)
                        raw = await response.content.read(4097)
                        if len(raw) > 4096: raise ValueError()
                        value = json.loads(raw)['asset_id']
                        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value): raise ValueError()
                        return value
        except (ClientError, TimeoutError) as exc: raise connection_error(exc) from None
        except (ValueError, KeyError, TypeError): raise CloudError('GPU_RESPONSE_INVALID', 502) from None

    async def generate(self, kind, data, output, *, assets=None, timeout=3600, validate=None):
        caps = await self.request('capabilities', {})
        if kind not in caps['kinds']: raise CloudError('GPU_CAPABILITY_UNAVAILABLE', 503)
        data = dict(data)
        for field, path in (assets or {}).items():
            with Path(path).open('rb') as source: digest = hashlib.file_digest(source, 'sha256').hexdigest()
            shared = next((a['asset_id'] for a in caps['shared_assets'] if a['sha256'] == digest), None)
            # Shared scenes must be deployed once; never silently re-upload large reference videos.
            if field == 'scene_asset' and shared is None: raise CloudError('GPU_SHARED_SCENE_MISSING', 503)
            data[field] = shared or await self.upload(path)
        task = await self.request('submit', {'request_id': uuid.uuid4().hex, 'kind': kind, 'input': data})
        deadline = time.monotonic() + timeout
        while task['status'] in ('queued', 'running'):
            if time.monotonic() >= deadline:
                await self.request('cancel', {'task_id': task['task_id']})
                raise CloudError('GPU_TASK_TIMEOUT', 504)
            await asyncio.sleep(1)
            task = await self.request('status', {'task_id': task['task_id']})
        return await self._download(task, output, validate=validate)

    async def download_task(self, task_id, output, *, validate=None):
        """Recover an existing result without submitting or charging another job."""
        task = await self.request('status', {'task_id': task_id})
        return await self._download(task, output, validate=validate)

    async def _download(self, task, output, *, validate=None):
        if task['status'] != 'succeeded' or len(task['outputs']) != 1:
            raise CloudError('GPU_TASK_FAILED', 502)
        output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement preserves an existing valid output if transport fails.
        temporary = None
        try:
            async with ClientSession(timeout=ClientTimeout(total=1800, sock_connect=30, sock_read=60), trust_env=False) as session:
                async with session.get(task['outputs'][0]['url'], allow_redirects=False) as response:
                    if response.status != 200: raise CloudError('GPU_DOWNLOAD_FAILED', 502)
                    size = 0
                    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=output.suffix, delete=False) as target:
                        temporary = Path(target.name)
                        async for chunk in response.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > 2147483648: raise CloudError('GPU_OUTPUT_TOO_LARGE', 502)
                            target.write(chunk)
                    if size == 0: raise CloudError('GPU_OUTPUT_EMPTY', 502)
            if validate is not None: validate(temporary)
            temporary.replace(output)
        except (ClientError, TimeoutError) as exc: raise connection_error(exc) from None
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)
        return task
