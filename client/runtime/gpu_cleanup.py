"""Acknowledge locally completed results; keep failed cleanup separate from generation."""
import hashlib
import json
from pathlib import Path

from runtime.cloud_service import CloudError


def _identity(api):
    return {'url': api.url, 'key_hash': hashlib.sha256(api.token.encode()).hexdigest()}


async def acknowledge_result(api, task_id, output):
    output = Path(output)
    if not output.is_file() or not output.stat().st_size:
        return
    marker = output.with_suffix(output.suffix + '.gpu-cleanup.json')
    try:
        temporary = marker.with_suffix('.tmp')
        temporary.write_text(json.dumps({**_identity(api), 'task_id': task_id}), encoding='utf-8')
        temporary.replace(marker)
        await api.request('ack', {'task_id': task_id})
        marker.unlink(missing_ok=True)
    except (CloudError, OSError):
        # The final local artifact remains usable; the background loop retries ACK only.
        pass


async def retry_pending(root, api):
    identity = _identity(api)
    for marker in Path(root).rglob('*.gpu-cleanup.json'):
        try:
            saved = json.loads(marker.read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                continue
            if any(saved.get(k) != v for k, v in identity.items()):
                continue
            await api.request('ack', {'task_id': saved['task_id']})
            marker.unlink(missing_ok=True)
        except (CloudError, OSError, ValueError, KeyError, TypeError):
            continue
