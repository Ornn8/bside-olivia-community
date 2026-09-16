"""Optional account connection. No letter/memory/world payloads cross this boundary."""
from __future__ import annotations

import asyncio
import json
import platform
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, ClientError
from original_client_setup_api import _dpapi_protect, _dpapi_unprotect


class CloudError(Exception):
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


def endpoint(value):
    if not isinstance(value, str) or len(value) > 500:
        raise CloudError('CLOUD_URL_INVALID', 400)
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError:
        raise CloudError('CLOUD_URL_INVALID', 400) from None
    if (not p.hostname or p.username or p.password or p.query or p.fragment
            or p.path not in ('', '/') or (port is not None and not 1 <= port <= 65535)
            or (p.scheme != 'https' and not (p.scheme == 'http' and p.hostname in ('127.0.0.1', 'localhost', '::1')))):
        raise CloudError('CLOUD_URL_INVALID', 400)
    return f'{p.scheme}://{p.netloc}'


class CloudService:
    def __init__(self, root: Path, *, version='unknown', errors=lambda: (),
                 protect=_dpapi_protect, unprotect=_dpapi_unprotect):
        self.path = Path(root) / 'cloud-session.dpapi'
        self.protect, self.unprotect = protect, unprotect
        self.version = version if re.fullmatch(r'[A-Za-z0-9_.:-]{1,40}', version) else 'unknown'
        self.os = platform.system().lower()
        if self.os not in ('windows', 'linux', 'darwin'): self.os = 'unknown'
        if self.os == 'darwin': self.os = 'macos'
        self.errors = errors
        self.state = {}
        self.lock = asyncio.Lock()
        self.last_error = ''
        self.last_sync = None
        self.next_sync = 0
        self.items = []

    async def load(self):
        try:
            if self.path.exists():
                if self.path.stat().st_size > 131072: raise ValueError()
                data = json.loads(await asyncio.to_thread(self.unprotect, self.path.read_text('utf-8')))
                endpoint(data['url'])
                if not isinstance(data.get('cookies'), dict) or not isinstance(data.get('queue', []), list): raise ValueError()
                self.state = data
        except Exception:
            self.state = {}
            self.last_error = 'CLOUD_LOCAL_SESSION_UNAVAILABLE'

    async def save(self):
        try:
            if not self.state:
                self.path.unlink(missing_ok=True)
                return
            cipher = await asyncio.to_thread(self.protect, json.dumps(self.state, ensure_ascii=True))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(cipher, encoding='utf-8')
            temporary.replace(self.path)
        except Exception:
            raise CloudError('CLOUD_LOCAL_SAVE_FAILED') from None

    def status(self):
        return {'status': 'READY', 'url': self.state.get('url', ''),
                'username': self.state.get('username', ''),
                'signed_in': bool(self.state.get('cookies', {}).get('sessionid')),
                'pending_reports': len(self.state.get('queue', [])),
                'last_sync': self.last_sync, 'error_code': self.last_error,
                'publications': self.items,
                'receipts': self.state.get('receipts', [])}

    async def call(self, path, payload=None, *, form=False):
        base = self.state['url']
        headers = {'Accept': 'application/json', 'Referer': base + '/', 'Origin': base}
        cookies = self.state.get('cookies', {})
        if payload is not None:
            headers['X-CSRFToken'] = cookies.get('csrftoken', '')
        try:
            async with ClientSession(timeout=ClientTimeout(total=10), cookies=cookies, trust_env=False) as session:
                options = {'data' if form else 'json': payload} if payload is not None else {}
                async with session.request('POST' if payload is not None else 'GET', base + path,
                                           headers=headers, allow_redirects=False, **options) as response:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        raw.extend(chunk)
                        if len(raw) > 2097152: raise CloudError('CLOUD_RESPONSE_INVALID')
                    if response.status == 401:
                        self.state['cookies'] = {}
                        self.state['queue'] = []
                        self.items = []
                        await self.save()
                        raise CloudError('CLOUD_AUTH_REQUIRED', 401)
                    if response.status >= 400 or response.status < 200 or response.status >= 300:
                        raise CloudError('CLOUD_RATE_LIMITED' if response.status == 429 else 'CLOUD_REQUEST_FAILED', response.status)
                    try:
                        data = json.loads(raw)
                        if not isinstance(data, dict): raise ValueError()
                    except (ValueError, UnicodeError):
                        raise CloudError('CLOUD_RESPONSE_INVALID') from None
                    for name in ('sessionid', 'csrftoken'):
                        if name in response.cookies:
                            cookies[name] = response.cookies[name].value
                    self.state['cookies'] = cookies
                    return data
        except (ClientError, asyncio.TimeoutError, OSError):
            raise CloudError('CLOUD_CONNECTION_FAILED') from None

    async def login(self, url, username, password, consent):
        url = endpoint(url)
        if consent is not True or not isinstance(username, str) or not 1 <= len(username) <= 150 or not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise CloudError('CLOUD_LOGIN_FIELDS_INVALID', 400)
        # Clear old credentials before changing endpoint; never forward them to a new host.
        await self.logout()
        self.state = {'url': url, 'cookies': {}, 'queue': [], 'receipts': []}
        try:
            await self.call('/api/v1/session')
            result = await self.call('/api/v1/login', {'username': username, 'password': password}, form=True)
            self.state['username'] = result['username']
            await self.save()
        except Exception:
            self.state = {}
            await self.save()
            raise
        self.last_error = ''
        self.next_sync = 0

    async def logout(self):
        try:
            if self.state.get('cookies', {}).get('sessionid'):
                await self.call('/api/v1/logout', {})
        except CloudError:
            pass
        finally:
            self.state = {}
            self.items = []
            self.last_sync = None
            self.last_error = ''
            await self.save()

    async def delete_cloud_data(self, password=None):
        # Do not retry destructive actions or silently reuse the login password.
        if password is not None:
            if not isinstance(password, str) or not 1 <= len(password) <= 1024:
                raise CloudError('CLOUD_LOGIN_FIELDS_INVALID', 400)
            await self.call('/api/v1/account/delete', {'confirm': True, 'password': password})
            await self.logout()
        else:
            await self.call('/api/v1/diagnostics/delete', {'confirm': True})
            self.state['queue'] = []
            self.state['receipts'] = []
            await self.save()

    def preview(self):
        codes = set()
        # Only the bounded runtime event buffer is supplied, never the letter store.
        for event in list(self.errors())[-100:]:
            if not isinstance(event, dict): continue
            for name in ('error_code', 'media_error_code'):
                value = event.get(name)
                if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,79}', value): codes.add(value)
        return [{'error_code': code, 'component': 'client', 'app_version': self.version, 'os_family': self.os}
                for code in sorted(codes)[:10]]

    async def enqueue(self, consent, codes):
        if not self.state.get('cookies', {}).get('sessionid'): raise CloudError('CLOUD_AUTH_REQUIRED', 401)
        if consent is not True or not isinstance(codes, list) or not 1 <= len(codes) <= 10:
            raise CloudError('CLOUD_REPORT_CONSENT_REQUIRED', 400)
        candidates = {item['error_code']: item for item in self.preview()}
        if any(not isinstance(code, str) or code not in candidates for code in codes):
            raise CloudError('CLOUD_REPORT_PREVIEW_EXPIRED', 409)
        queue = self.state.setdefault('queue', [])
        if len(queue) + len(set(codes)) > 20: raise CloudError('CLOUD_REPORT_QUEUE_FULL', 409)
        for code in set(codes):
            if any(item['payload']['error_code'] == code for item in queue): continue
            queue.append({'payload': dict(candidates[code], report_id=uuid.uuid4().hex, consent=True),
                          'created': time.time(), 'attempts': 0})
        await self.save()
        self.next_sync = 0

    async def sync(self):
        if not self.state.get('cookies', {}).get('sessionid'): return
        self.next_sync = time.time() + 900
        try:
            await self.call('/api/v1/me')
            await self.call('/api/v1/presence', {'app_version': self.version, 'os_family': self.os})
            result = await self.call('/api/v1/publications')
            self.items = result.get('items', [])[:50]
            self.last_sync = time.time()
            self.last_error = ''
            queue = self.state.setdefault('queue', [])
            # Bound one synchronization to one report, even after an offline backlog.
            for item in tuple(queue)[:1]:
                if item['attempts'] >= 3 or time.time() - item['created'] > 86400:
                    queue.remove(item)
                    self.last_error = 'CLOUD_REPORT_RETRY_EXHAUSTED'
                    continue
                item['attempts'] += 1
                await self.save()  # Count attempted delivery even if the process is interrupted.
                result = await self.call('/api/v1/diagnostics', item['payload'])
                self.state.setdefault('receipts', []).append({'id': result['id'], 'error_code': item['payload']['error_code']})
                self.state['receipts'] = self.state['receipts'][-20:]
                queue.remove(item)
                await self.save()
            if queue:
                self.next_sync = time.time() + 300
            await self.save()
        except CloudError as exc:
            self.last_error = exc.code
            self.next_sync = time.time() + 300

    async def run(self):
        while True:
            async with self.lock:
                if time.time() >= self.next_sync:
                    try:
                        await self.sync()
                    except Exception:
                        self.last_error = 'CLOUD_UNAVAILABLE'
                        self.next_sync = time.time() + 900
            await asyncio.sleep(5)
