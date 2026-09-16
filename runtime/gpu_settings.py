"""User-configured generation endpoint, stored with Windows account encryption."""
import json
import os
from pathlib import Path
from original_client_setup_api import _dpapi_protect, _dpapi_unprotect
from runtime.cloud_service import CloudError, endpoint
from runtime.remote_generation import RemoteGeneration


class GPUSettings:
    def __init__(self, root, *, environment=None, protect=_dpapi_protect, unprotect=_dpapi_unprotect):
        self.path = Path(root) / 'gpu-connection.dpapi'
        self.environment = os.environ if environment is None else environment
        self.protect, self.unprotect = protect, unprotect
        self.error = ''

    def candidate(self, route, url, key):
        if route not in ('local', 'remote') or not isinstance(url, str) or not isinstance(key, str):
            raise CloudError('GPU_SETTINGS_INVALID', 400)
        url = endpoint(url.strip()) if url.strip() else ''
        if not key and url == self.environment.get('OLIVIA_GPU_API_URL', ''):
            key = self.environment.get('OLIVIA_GPU_API_KEY', '')
        if len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise CloudError('GPU_KEY_INVALID', 400)
        if route == 'remote' and (not url or not key):
            raise CloudError('GPU_NOT_CONFIGURED', 400)
        return {'route':route, 'url':url, 'key':key}

    def apply(self, value):
        self.environment['OLIVIA_GPU_ROUTE'] = value['route']
        for name, field in (('OLIVIA_GPU_API_URL','url'), ('OLIVIA_GPU_API_KEY','key')):
            if value[field]: self.environment[name] = value[field]
            else: self.environment.pop(name, None)

    def load(self):
        if not self.path.exists(): return
        try:
            if self.path.stat().st_size > 65536: raise ValueError()
            value = json.loads(self.unprotect(self.path.read_text(encoding='utf-8')))
            self.apply(self.candidate(**value))
        except Exception:
            self.apply({'route':'local','url':'','key':''})
            self.error = 'GPU_SETTINGS_UNAVAILABLE'

    def save(self, route, url, key):
        value = self.candidate(route, url, key)
        try:
            cipher = self.protect(json.dumps(value))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(cipher, encoding='utf-8')
            temporary.replace(self.path)
        except Exception:
            raise CloudError('GPU_SETTINGS_SAVE_FAILED') from None
        self.apply(value)
        self.error = ''
        return self.status()

    def clear(self):
        try: self.path.unlink(missing_ok=True)
        except OSError: raise CloudError('GPU_SETTINGS_SAVE_FAILED') from None
        self.apply({'route':'local','url':'','key':''})
        self.error = ''
        return self.status()

    def status(self):
        return {'status':'OK', 'route':self.environment.get('OLIVIA_GPU_ROUTE','local'),
                'url':self.environment.get('OLIVIA_GPU_API_URL',''),
                'has_key':bool(self.environment.get('OLIVIA_GPU_API_KEY')),
                'error_code':self.error}

    async def test(self, url, key):
        value = self.candidate('remote', url, key)
        result = await RemoteGeneration(value['url'], value['key']).request('capabilities', {})
        return {'status':'OK', 'kinds':result['kinds']}
