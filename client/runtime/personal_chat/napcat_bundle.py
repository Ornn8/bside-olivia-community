"""Retrieve the pinned QQ distribution from R2 using a fresh server ticket."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import urllib.request
from urllib.parse import urlsplit
import zipfile

TICKET_URL = 'https://175.24.191.6/v1/components/qq/download'
R2_HOST = '3fa206f49fd071a9eff9a1c9905208dd.r2.cloudflarestorage.com'
BUNDLE_SHA256 = 'b4e35b6dfeb46c503cc46332f8f8fc376f7b7ae394acc25d51410290eebfb727'
BUNDLE_SIZE = 427362524
_LOCK = threading.Lock()


def _ticket():
    from runtime.remote_generation import gpu_tls_context
    from .napcat_installer import NapCatSetupError
    request = urllib.request.Request(TICKET_URL, headers={'User-Agent': 'Olivia-QQ-bootstrap/1'})
    with urllib.request.urlopen(request, context=gpu_tls_context(), timeout=20) as response:
        if response.geturl() != TICKET_URL:
            raise NapCatSetupError('NAPCAT_SOURCE_INVALID')
        value = json.loads(response.read(16385))
    url = value.get('url', '')
    parsed = urlsplit(url)
    if (value.get('sha256') != BUNDLE_SHA256 or value.get('size_bytes') != BUNDLE_SIZE
            or parsed.scheme != 'https' or parsed.hostname != R2_HOST
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not parsed.path.endswith('/Olivia-QQ-v4.18.28-full.zip')):
        raise NapCatSetupError('NAPCAT_SOURCE_INVALID')
    return url


def ensure_bundle(data_root: Path):
    from .napcat_installer import _root, _download_archive, NAPCAT_ASSET, NAPCAT_SHA256, NapCatSetupError
    from .napcat_dependencies import QQ_SHA256, EXTRACTOR_SHA256, _matches
    root = _root(data_root)
    targets = {NAPCAT_ASSET: (root / NAPCAT_ASSET, NAPCAT_SHA256),
               'qq-9.9.31.exe': (root / 'dependencies/qq-9.9.31.exe', QQ_SHA256),
               '7zr-26.03.exe': (root / 'dependencies/7zr-26.03.exe', EXTRACTOR_SHA256)}
    with _LOCK:
        if all(_matches(path, digest) for path, digest in targets.values()):
            return
        bundle = root / 'Olivia-QQ-v4.18.28-full.zip'
        if not _matches(bundle, BUNDLE_SHA256):
            for attempt in range(2):
                try:
                    _download_archive(bundle, url=_ticket(), sha256=BUNDLE_SHA256, limit=BUNDLE_SIZE)
                    break
                except Exception:
                    if attempt:
                        raise NapCatSetupError('NAPCAT_R2_DOWNLOAD_FAILED') from None
        try:
            with zipfile.ZipFile(bundle) as archive:
                for name, (target, digest) in targets.items():
                    if _matches(target, digest):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Extract only exact pinned entries, never archive paths.
                    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
                        stage = Path(temporary) / name
                        with archive.open(name) as source, stage.open('wb') as output:
                            import shutil
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                        if not _matches(stage, digest):
                            raise NapCatSetupError('NAPCAT_ARCHIVE_HASH_MISMATCH')
                        stage.replace(target)
        except (OSError, KeyError, zipfile.BadZipFile):
            raise NapCatSetupError('NAPCAT_ARCHIVE_INVALID') from None
