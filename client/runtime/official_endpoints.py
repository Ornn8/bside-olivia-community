"""Managed service addresses and the exact legacy-IP upgrade boundary."""
import json
import os
from pathlib import Path
import tempfile

API_ORIGIN = 'https://api.bside-moon.cn'
API_BASE = API_ORIGIN + '/v1'
DOWNLOAD_BASE = 'https://download.bside-moon.cn/installers/'
WEBSITE = 'https://bside-moon.cn/'
LEGACY_API_ORIGIN = 'https://175.24.191.6'
LEGACY_API_BASE = LEGACY_API_ORIGIN + '/v1'
COMPONENT_COS_HOST = 'olivia-files-1400665687.cos.ap-guangzhou.myqcloud.com'
COMPONENT_COS_HOSTS = frozenset({
    COMPONENT_COS_HOST,
    'iupaper-1387429524.cos.ap-guangzhou.myqcloud.com',
})
COMPONENT_COS_PREFIX = '/olivia/components/'


def canonical_api_base(value: str) -> str:
    return API_BASE if value.rstrip('/') == LEGACY_API_BASE else value


def canonical_api_origin(value: str) -> str:
    return API_ORIGIN if value.rstrip('/') == LEGACY_API_ORIGIN else value


def migrate_saved_api_url(path: Path) -> None:
    """Rewrite only the public address; keep encrypted key files and their hashes.

    Runtime normalization still works if a read-only install prevents saving.
    """
    temporary = None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(payload, dict) or payload.get('schema_version') not in (1, 2, 3)
                or not isinstance(payload.get('base_url'), str)
                or payload['base_url'].strip().rstrip('/') != LEGACY_API_BASE):
            return
        payload['base_url'] = API_BASE
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.llm-domain-', suffix='.staging', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, UnicodeError, ValueError):
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
