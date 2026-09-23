"""Repair the two DLLs omitted by NapCat v4.18.28's Windows Node archive.

Use the exact QQ installer/hash from upstream release-publish.yml. Never run
the QQ installer: extract only its pinned 7z resource and two verified DLLs.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

QQ_URL = 'https://github.com/Rodert/qq-versions/releases/download/qq-packages-20260528-3e8913a2/QQ_9.9.31_260528_x64_01.exe'
QQ_SHA256 = '0beb5cb4ff776cba822caa0abadbd53f5892f12628a86dabea1b372c82c086bc'
EXTRACTOR_URL = 'https://github.com/ip7z/7zip/releases/download/26.03/7zr.exe'
EXTRACTOR_SHA256 = 'ad4c82fadcbdf93c03b4fc440f300509c7d60c5c2f4d183e35d9d70d6957037d'
# .rsrc/2052/MSI/101 in the SHA-256-pinned PE above.
QQ_RESOURCE_OFFSET = 160568
QQ_RESOURCE_SIZE = 307268112
DLL_HASHES = {
    'crypto.dll': '2a143d944cd80ba373242deacd06ad47523c07191a4a322e4336e62062d2f5f3',
    'ssl.dll': 'fbadd7a295e8d932295c5b59c086b1a9074f61f347694674b4c76ecff5c5965c',
}


def _matches(path: Path, digest: str) -> bool:
    try:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == digest
    except OSError:
        return False


def ensure_dependencies(data_root: Path, shell: Path) -> None:
    from .napcat_installer import _download_archive, _root, NapCatSetupError

    if all(_matches(shell / name, digest) for name, digest in DLL_HASHES.items()):
        return
    cache = _root(data_root) / 'dependencies'
    cache.mkdir(parents=True, exist_ok=True)
    try:
        for name, url, digest, limit in [
            ('qq-9.9.31.exe', QQ_URL, QQ_SHA256, 320 * 1024 * 1024),
            ('7zr-26.03.exe', EXTRACTOR_URL, EXTRACTOR_SHA256, 2 * 1024 * 1024),
        ]:
            path = cache / name
            if not _matches(path, digest):
                from .napcat_bundle import ensure_bundle
                ensure_bundle(data_root)
            if not _matches(path, digest):
                raise NapCatSetupError('NAPCAT_DEPENDENCY_HASH_MISMATCH')
        with tempfile.TemporaryDirectory(prefix='extract-', dir=cache) as temporary:
            stage = Path(temporary)
            archive = stage / 'qq.7z'
            with (cache / 'qq-9.9.31.exe').open('rb') as source, archive.open('wb') as target:
                source.seek(QQ_RESOURCE_OFFSET)
                remaining = QQ_RESOURCE_SIZE
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise NapCatSetupError('NAPCAT_DEPENDENCY_HASH_MISMATCH')
                    target.write(chunk)
                    remaining -= len(chunk)
            result = subprocess.run(
                [str(cache / '7zr-26.03.exe'), 'e', str(archive), '-y', f'-o{stage}',
                 *[f'Files/versions/9.9.31-49738/resources/app/{name}' for name in DLL_HASHES]],
                capture_output=True, timeout=180, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if result.returncode != 0:
                raise NapCatSetupError('NAPCAT_DEPENDENCY_EXTRACT_FAILED')
            if not all(_matches(stage / name, digest) for name, digest in DLL_HASHES.items()):
                raise NapCatSetupError('NAPCAT_DEPENDENCY_HASH_MISMATCH')
            for name in DLL_HASHES:
                # Stage beside the destination so replacement stays on one filesystem.
                fd, pending = tempfile.mkstemp(prefix=f'.{name}-', dir=shell)
                try:
                    with os.fdopen(fd, 'wb') as output, (stage / name).open('rb') as source:
                        shutil.copyfileobj(source, output)
                    os.replace(pending, shell / name)
                finally:
                    Path(pending).unlink(missing_ok=True)
    except NapCatSetupError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise NapCatSetupError('NAPCAT_DEPENDENCY_REPAIR_FAILED') from exc
