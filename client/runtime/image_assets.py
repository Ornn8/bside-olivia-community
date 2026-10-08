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
from urllib.parse import quote, unquote, urlsplit
from weakref import WeakKeyDictionary
import zipfile

from aiohttp import ClientSession, ClientTimeout, TCPConnector, ClientError
from runtime.cloud_service import CloudError
from runtime.remote_generation import gpu_tls_context
from runtime.official_endpoints import COMPONENT_COS_HOSTS, COMPONENT_COS_PREFIX, canonical_api_origin

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
                          parsed.hostname in COMPONENT_COS_HOSTS
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


def validate_entry(kind, asset_id, entry):
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
    return dict(entry)


def _wardrobe_index(data_root):
    return _cache_path(data_root, 'wardrobe', {'sha256':'catalog', 'filename':'index.json'})


def _ui_index(data_root):
    return _cache_path(data_root, 'ui', {'sha256':'catalog', 'filename':'index.json'})


def _read_index(path):
    try:
        with path.open('rb') as source:
            raw = source.read(2097153)
        if len(raw) > 2097152:
            raise ValueError()
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _remember_ui(data_root, asset_id, entry):
    """Keep the cloud's current entry so the picture still shows offline later."""
    entries = _read_index(_ui_index(data_root))
    if entries.get(asset_id) == entry:
        return
    entries[asset_id] = entry
    raw = json.dumps(entries, ensure_ascii=False, sort_keys=True).encode('utf-8')
    if len(raw) <= 2097152:
        _save_builtin(raw, _ui_index(data_root), {'size_bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest()})


def _ticket_entry(kind, asset_id, ticket):
    """The entry a download ticket describes, pinned to the storage object for its digest."""
    try:
        filename = unquote(urlsplit(ticket['url']).path.rsplit('/', 1)[-1])
        entry = {'content_type': ticket['content_type'], 'filename': filename,
                 'key': f"distribution/olivia-images/{kind}/{ticket['sha256']}/{filename}",
                 'sha256': ticket['sha256'], 'size_bytes': ticket['size_bytes']}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CloudError('IMAGE_ASSET_INVALID', 502) from None
    entry = validate_entry(kind, asset_id, entry)
    _validate_download_url(ticket['url'], entry)
    return entry


def image_entry(data_root, kind, asset_id):
    if kind == 'ui' and data_root is not None:
        known = _read_index(_ui_index(data_root)).get(asset_id)
        if known is not None:
            try:
                return validate_entry(kind, asset_id, known)
            except CloudError:
                pass
    if kind == 'wardrobe' and data_root is not None:
        try:
            path = _wardrobe_index(data_root)
            with path.open('rb') as source:
                raw = source.read(2097153)
            if len(raw) > 2097152:
                raise ValueError()
            entries = json.loads(raw)
            if asset_id in entries:
                return validate_entry(kind, asset_id, entries[asset_id])
        except (OSError, ValueError, TypeError):
            pass  # A fresh server catalog will repair a missing/corrupt index.
    try:
        return validate_entry(kind, asset_id, _catalog()[kind][asset_id])
    except (KeyError, TypeError):
        raise CloudError('IMAGE_ASSET_NOT_FOUND', 404) from None


def save_wardrobe_catalog(data_root, state):
    from runtime.wardrobe import CLOUD_CATALOG_PROTOCOL, validate_cloud_state
    if state.get('catalog_protocol') != CLOUD_CATALOG_PROTOCOL:
        return
    validate_cloud_state(state)
    if data_root is None:
        raise CloudError('IMAGE_CACHE_UNAVAILABLE', 503)
    entries = {look['look_id']: look['image_asset'] for style in state['wardrobe_styles'] for look in style['looks']}
    raw = json.dumps(entries, ensure_ascii=False, sort_keys=True).encode('utf-8')
    if len(raw) > 2097152:
        raise CloudError('IMAGE_CATALOG_INVALID', 503)
    _save_builtin(raw, _wardrobe_index(data_root), {'size_bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest()})


async def _cloud_ui_entry(base, asset_id):
    """Interface pictures are cloud-owned: the relay's current entry wins over the
    copy shipped with this version, so new or replaced pictures need no client release."""
    async with ClientSession(timeout=ClientTimeout(total=20), trust_env=False,
            connector=TCPConnector(ssl=gpu_tls_context())) as session:
        async with session.get(base + '/v1/components/images/ui/' + asset_id, allow_redirects=False,
                headers={'X-Olivia-Component-Storage': 'cos-v2'}) as response:
            if response.status != 200:
                raise CloudError('IMAGE_ASSET_UNAVAILABLE', 503)
            raw = await response.content.read(8193)
    if len(raw) > 8192:
        raise CloudError('IMAGE_ASSET_INVALID', 502)
    try:
        return _ticket_entry('ui', asset_id, json.loads(raw))
    except ValueError:
        raise CloudError('IMAGE_ASSET_INVALID', 502) from None


async def ensure_image(data_root, kind, asset_id, *, base_url=None):
    """No eager download; verified cache works offline and across patch versions."""
    from runtime.gpu_settings import GPU_BASE
    base = canonical_api_origin((base_url or os.environ.get('OLIVIA_GPU_API_URL') or GPU_BASE).rstrip('/'))
    entry = None
    if kind == 'ui' and data_root is not None and re.fullmatch(r'[a-z0-9-]{1,80}', asset_id):
        try:
            entry = await _cloud_ui_entry(base, asset_id)
            await asyncio.to_thread(_remember_ui, data_root, asset_id, entry)
        except (ClientError, TimeoutError, OSError, CloudError):
            entry = None  # offline: the remembered or shipped entry still serves a cached copy
    if entry is None:
        entry = image_entry(data_root, kind, asset_id)
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
        ticket_url = base + '/v1/components/images/' + kind + '/' + asset_id
        target.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            stage = None
            try:
                async with ClientSession(timeout=ClientTimeout(total=45), trust_env=False,
                        connector=TCPConnector(ssl=gpu_tls_context())) as session:
                    async with session.get(ticket_url, allow_redirects=False,
                            headers={'X-Olivia-Component-Storage': 'cos-v2'}) as response:
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
