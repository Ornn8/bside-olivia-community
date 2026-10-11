"""Cloud generation always uses the Olivia account key; there is no custom endpoint."""
import os
from pathlib import Path
from runtime.official_endpoints import API_ORIGIN

GPU_BASE = API_ORIGIN


class GPUSettings:
    def __init__(self, root, *, environment=None, account_key=None):
        # Older releases stored a custom endpoint here. It is ignored, never read.
        self.path = Path(root) / 'gpu-connection.dpapi'
        self.environment = os.environ if environment is None else environment
        self.account_key = account_key
        self.error = ''

    def _key(self):
        try:
            key = self.account_key() if callable(self.account_key) else None
        except Exception:
            return ''
        return key if isinstance(key, str) and key.startswith('olivia-') else ''

    def load(self):
        """Point generation at the Olivia cloud with the current account key."""
        key = self._key()
        if key:
            self.environment.update(OLIVIA_GPU_ROUTE='remote', OLIVIA_GPU_API_URL=GPU_BASE,
                                    OLIVIA_GPU_API_KEY=key)
        else:
            for name in ('OLIVIA_GPU_ROUTE', 'OLIVIA_GPU_API_URL', 'OLIVIA_GPU_API_KEY'):
                self.environment.pop(name, None)

    def status(self):
        self.load()
        return {'status': 'OK', 'route': 'remote', 'url': GPU_BASE,
                'has_key': bool(self.environment.get('OLIVIA_GPU_API_KEY')),
                'error_code': '' if self.environment.get('OLIVIA_GPU_API_KEY') else 'RELAY_NOT_CONFIGURED'}
