"""User-configured generation endpoint, stored with Windows account encryption."""
import json
import os
import secrets
import re
from pathlib import Path
from original_client_setup_api import _dpapi_protect, _dpapi_unprotect, LLMSetupError
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
        except FileNotFoundError:
            raise CloudError('GPU_ENCRYPTION_TOOL_MISSING') from None
        except LLMSetupError:
            raise CloudError('GPU_ENCRYPTION_FAILED') from None
        except Exception:
            raise CloudError('GPU_ENCRYPTION_FAILED') from None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(cipher, encoding='utf-8')
            temporary.replace(self.path)
        except PermissionError:
            raise CloudError('GPU_SETTINGS_PERMISSION_DENIED') from None
        except OSError:
            raise CloudError('GPU_SETTINGS_WRITE_FAILED') from None
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

    async def claim(self, url):
        url = endpoint(url.strip())
        path = self.path.with_name('gpu-anonymous-identities.dpapi')
        try:
            identities = {}
            if path.exists():
                if path.stat().st_size > 65536:
                    raise ValueError()
                identities = json.loads(self.unprotect(path.read_text(encoding='utf-8')))
                if not isinstance(identities, dict) or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', v) for v in identities.values()):
                    raise ValueError()
            if url not in identities:
                if len(identities) >= 16:
                    raise ValueError()
                identities[url] = secrets.token_urlsafe(32)
                cipher = self.protect(json.dumps(identities))
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(cipher, encoding='utf-8')
                temporary.replace(path)
        except Exception:
            # Do not issue a new identity if persisted identity cannot be read/saved.
            raise CloudError('GPU_IDENTITY_STORAGE_FAILED') from None
        result = await RemoteGeneration(url).claim_key(identities[url])
        saved = self.save('remote', url, result['key'])
        return dict(saved, anonymous_owner=result['owner'])
