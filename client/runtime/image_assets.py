"""Fetch one pinned distribution image into durable user data."""
import asyncio
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import quote, urlsplit
from weakref import WeakKeyDictionary
import zipfile

from aiohttp import ClientSession, ClientTimeout, TCPConnector, ClientError
from runtime.cloud_service import CloudError
from runtime.remote_generation import gpu_tls_context
from runtime.official_endpoints import COMPONENT_COS_HOST, COMPONENT_COS_PREFIX, canonical_api_origin

R2_HOST = '3fa206f49fd071a9eff9a1c9905208dd.r2.cloudflarestorage.com'
R2_BUCKET = 'vocal-backlog'
_LOCKS = WeakKeyDictionary()


@lru_cache(maxsize=1)
def _catalog():
    try:
        value = json.loads(Path(__file__).with_suffix('.json').read_text('utf-8'))
        if value['schema_version'] != 'olivia.image-assets.v1':
            raise ValueError()
        return value['assets']
    except (OSError, ValueError, KeyError, TypeError):
        raise CloudError('IMAGE_CATALOG_INVALID', 503) from None


def _validate_download_url(url, entry):
    try:
        parsed = urlsplit(url)
        source_matches = (parsed.hostname == R2_HOST
                          and parsed.path == '/' + R2_BUCKET + '/' + quote(entry['key'], safe='/')) or (
                          parsed.hostname == COMPONENT_COS_HOST
                          and parsed.path == COMPONENT_COS_PREFIX + quote(entry['key'], safe='/'))
        valid = (parsed.scheme == 'https' and source_matches
                 and not parsed.username and not parsed.password and not parsed.fragment
                  and parsed.port in (None, 443))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise CloudError('IMAGE_ASSET_INVALID', 502)


def _cache_path(data_root, kind, entry):
    root = Path(data_root).absolute()
    target = root / 'image-assets' / kind / entry['sha256'] / entry['filename']
    for path in (root, *reversed(target.relative_to(root).parents)):
        path = path if path == root else root / path
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise CloudError('IMAGE_CACHE_INVALID', 503)
    if target.is_symlink() or target.exists() and getattr(target.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise CloudError('IMAGE_CACHE_INVALID', 503)
    return target


def _matches(path, entry):
    try:
        if path.stat().st_size != entry['size_bytes']:
            return False
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == entry['sha256']
    except OSError:
        return False


def _image_header(raw, content_type):
    return {
        'image/png': raw.startswith(b'\x89PNG\r\n\x1a\n'),
        'image/gif': raw.startswith((b'GIF87a', b'GIF89a')),
        'image/jpeg': raw.startswith(b'\xff\xd8\xff'),
        'image/webp': raw[:4] == b'RIFF' and raw[8:12] == b'WEBP',
    }.get(content_type, False)


def _builtin_image(data_root, entry):
    """Default line art is installed; an image-free patch reuses the native ZIP."""
    from installer.full_patch import DEFAULT_STICKER_FILES
    if entry['filename'] not in DEFAULT_STICKER_FILES:
        return None
    source = Path(__file__).parent / 'letter_stickers' / entry['filename']
    if _matches(source, entry):
        return source.read_bytes()
    root = Path(os.environ.get('OLIVIA_INSTALL_ROOT') or Path(data_root).parent)
    try:
        version = json.loads((root / '.olivia-full-patch.json').read_text('utf-8'))['client_version']
        if not isinstance(version,str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,5}',version):
            return None
        with zipfile.ZipFile(root / 'app' / version / 'resources/feapp.dat') as archive:
            name = 'assets/letter-stickers/' + entry['filename']
            if archive.getinfo(name).file_size == entry['size_bytes']:
                return archive.read(name)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        pass
    return None


def _save_builtin(raw, target, entry):
    if raw is None or len(raw)!=entry['size_bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
        return False
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,suffix='.tmp',delete=False) as output:
            stage=Path(output.name)
            output.write(raw);output.flush();os.fsync(output.fileno())
        stage.replace(target)
    finally:
        if stage: stage.unlink(missing_ok=True)
    return True


async def ensure_image(data_root, kind, asset_id, *, base_url=None):
    """No eager download; verified cache works offline and across patch versions."""
    try:
        entry = _catalog()[kind][asset_id]
    except (KeyError, TypeError):
        raise CloudError('IMAGE_ASSET_NOT_FOUND', 404) from None
    try:
        filename, digest = entry['filename'], entry['sha256']
        if (kind not in {'stickers','wardrobe','ui'} or not re.fullmatch(r'[a-z0-9-]{1,80}', asset_id)
                or not re.fullmatch(r'[A-Za-z0-9_-]+\.(?:png|gif|jpeg|webp)', filename)
                or not re.fullmatch(r'[a-f0-9]{64}', digest)
                or type(entry['size_bytes']) is not int or not 0 < entry['size_bytes'] <= 15*1024*1024
                or entry['content_type'] not in {'image/png','image/gif','image/jpeg','image/webp'}
                or entry['key'] != f'distribution/olivia-images/{kind}/{digest}/{filename}'):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise CloudError('IMAGE_CATALOG_INVALID', 503) from None
    if data_root is None:
        raise CloudError('IMAGE_CACHE_UNAVAILABLE', 503)
    target = _cache_path(data_root, kind, entry)
    locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    async with locks.setdefault(str(target), asyncio.Lock()):
        if await asyncio.to_thread(_matches, target, entry):
            return target
        if kind=='stickers':
            raw=await asyncio.to_thread(_builtin_image,data_root,entry)
            if await asyncio.to_thread(_save_builtin,raw,target,entry):
                return target
            # Other styles come only from packs the user installed from the download page.
            from runtime.letter_stickers.packs import installed
            pack_file=(await asyncio.to_thread(installed,data_root)).get(asset_id)
            if pack_file is None:
                raise CloudError('STICKER_PACK_NOT_INSTALLED', 404)
            return pack_file
        from runtime.gpu_settings import GPU_BASE
        base = canonical_api_origin((base_url or os.environ.get('OLIVIA_GPU_API_URL') or GPU_BASE).rstrip('/'))
        ticket_url = base + '/v1/components/images/' + kind + '/' + asset_id
        target.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            stage = None
            try:
                async with ClientSession(timeout=ClientTimeout(total=45), trust_env=False,
                        connector=TCPConnector(ssl=gpu_tls_context())) as session:
                    async with session.get(ticket_url, allow_redirects=False) as response:
                        if response.status != 200:
                            raise CloudError('IMAGE_ASSET_UNAVAILABLE', 503)
                        raw = await response.content.read(8193)
                        if len(raw) > 8192:
                            raise CloudError('IMAGE_ASSET_INVALID', 502)
                        ticket = json.loads(raw)
                        if any(ticket.get(key) != entry[key] for key in ('sha256', 'size_bytes', 'content_type')):
                            raise CloudError('IMAGE_ASSET_INVALID', 502)
                        url = ticket['url']
                        _validate_download_url(url, entry)
                    # Separate object GET carries no account credential. Redirects are forbidden.
                    async with session.get(url, allow_redirects=False) as response:
                        if response.status != 200:
                            raise CloudError('IMAGE_ASSET_UNAVAILABLE', 503)
                        with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.tmp', delete=False) as output:
                            stage = Path(output.name)
                            size, digest, header = 0, hashlib.sha256(), b''
                            async for chunk in response.content.iter_chunked(65536):
                                size += len(chunk)
                                if size > entry['size_bytes']:
                                    raise CloudError('IMAGE_ASSET_INVALID', 502)
                                if len(header) < 12:
                                    header = (header + chunk)[:12]
                                digest.update(chunk)
                                output.write(chunk)
                            if (size != entry['size_bytes'] or digest.hexdigest() != entry['sha256']
                                    or not _image_header(header, entry['content_type'])):
                                raise CloudError('IMAGE_ASSET_INVALID', 502)
                            output.flush()
                            os.fsync(output.fileno())
                stage.replace(target)
                return target
            except (ClientError, TimeoutError, OSError, ValueError, KeyError, TypeError, CloudError):
                if attempt:
                    raise CloudError('IMAGE_ASSET_UNAVAILABLE', 503) from None
            finally:
                if stage:
                    stage.unlink(missing_ok=True)
