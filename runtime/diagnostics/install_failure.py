"""Bounded installer evidence; never serialize exception text, URLs or paths."""
import errno
import re
from urllib.error import HTTPError, URLError
import zipfile

STAGES = frozenset({'prepare', 'download', 'offline_copy', 'verify_file', 'stage_copy', 'extract', 'verify_tree', 'dependencies', 'activate', 'cleanup'})
SOURCES = frozenset({'auto', 'domestic', 'official', 'local', 'offline-package'})
KINDS = frozenset({'http', 'network', 'timeout', 'disk_full', 'permission', 'path_too_long', 'file_missing', 'file_locked', 'io', 'archive', 'validation', 'unexpected'})

def project_install_failure(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for name, allowed in [('stage', STAGES), ('source', SOURCES), ('kind', KINDS)]:
        if isinstance(value.get(name), str) and value[name] in allowed:
            result[name] = value[name]
    for name in ('errno', 'winerror', 'http_status'):
        number = value.get(name)
        if type(number) is int and 0 <= number <= 65535:
            result[name] = number
    # Only an identifier from the package manifest, never a user filesystem path.
    identifier = value.get('file_id')
    if isinstance(identifier, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}', identifier):
        result['file_id'] = identifier
    return result

def install_failure(error, *, stage, source=None, file_id=None):
    cause = error.reason if isinstance(error, URLError) and isinstance(error.reason, BaseException) else error
    number, win = getattr(cause, 'errno', None), getattr(cause, 'winerror', None)
    if isinstance(error, HTTPError): kind = 'http'
    elif isinstance(cause, TimeoutError): kind = 'timeout'
    elif number == errno.ENOSPC or win == 112: kind = 'disk_full'
    elif win in (32, 33): kind = 'file_locked'
    elif isinstance(cause, PermissionError): kind = 'permission'
    elif number == errno.ENAMETOOLONG or win == 206: kind = 'path_too_long'
    elif isinstance(cause, FileNotFoundError): kind = 'file_missing'
    elif isinstance(error, URLError): kind = 'network'
    elif isinstance(cause, OSError): kind = 'io'
    elif isinstance(cause, zipfile.BadZipFile): kind = 'archive'
    elif isinstance(cause, ValueError): kind = 'validation'
    else: kind = 'unexpected'
    return project_install_failure(dict(stage=stage, source=source, file_id=file_id, kind=kind,
                                       errno=number, winerror=win, http_status=getattr(error, 'code', None)))
