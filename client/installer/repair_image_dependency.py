"""Offline, reversible Pillow repair for the managed Windows Python runtime."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile
import sys

WHEEL = 'pillow-12.3.0-cp312-cp312-win_amd64.whl'
SHA256 = 'a2b55dd6b2a4c4b7d87ffa56bdb33fdc5fdb9a462173861a7bc097f17d91cb09'
PROBE = ('import io; from PIL import Image; b=io.BytesIO(); '
         'Image.new("RGB",(2,2)).save(b,format="PNG"); b.seek(0); '
         'im=Image.open(b); im.load(); assert im.size==(2,2)')


def probe(executable, extra=None):
    code = ('import sys; sys.path.insert(0,' + repr(str(extra)) + '); ' if extra else '') + PROBE
    result = subprocess.run([str(executable), '-I', '-c', code], capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return result.returncode == 0


def repair(runtime, wheel):
    raw_runtime = Path(runtime).absolute()
    for part in (raw_runtime, *raw_runtime.parents):
        if part.exists() and (part.is_symlink() or getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('REPAIR_RUNTIME_LINK_REFUSED')
    runtime, wheel = Path(runtime).resolve(), Path(wheel).resolve()
    executable = runtime / 'python.exe'
    pth = runtime / 'python312._pth'
    if runtime.name != 'python-3.12.10-embed-amd64' or not executable.is_file() or not pth.is_file():
        raise ValueError('REPAIR_RUNTIME_INVALID')
    if probe(executable):
        return 'ALREADY_HEALTHY'
    raw = wheel.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError('REPAIR_WHEEL_HASH_MISMATCH')
    # A fresh directory avoids overwriting loaded DLLs or existing packages.
    target = Path(tempfile.mkdtemp(prefix='image-dependency-', dir=runtime))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for member in archive.infolist():
            relative = Path(member.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in member.filename or ':' in member.filename:
                raise ValueError('REPAIR_WHEEL_PATH_INVALID')
        archive.extractall(target)
    if not probe(executable, target):
        raise RuntimeError('REPAIR_DEPENDENCY_PROBE_FAILED')
    before = pth.read_bytes()
    backup = runtime / ('python312._pth.before-image-repair-' + target.name)
    backup.write_bytes(before)
    updated = (target.name + '\n').encode('utf-8') + before
    temporary = target / 'python312._pth.new'
    temporary.write_bytes(updated)
    try:
        os.replace(temporary, pth)
        if not probe(executable):
            raise RuntimeError('REPAIR_ACTIVATION_FAILED')
    except BaseException:
        temporary.write_bytes(before)
        os.replace(temporary, pth)
        raise
    return 'REPAIRED'


def ensure_bundled_image_dependency():
    """An old updater can install this payload; repair on its first startup."""
    runtime = Path(sys.executable).parent
    wheel = Path(__file__).with_name(WHEEL)
    if os.name != 'nt' or runtime.name != 'python-3.12.10-embed-amd64' or not wheel.is_file():
        return
    repair(runtime, wheel)


if __name__ == '__main__':
    import sys
    try:
        result = repair(sys.argv[1], Path(__file__).with_name(WHEEL))
        print(json.dumps({'status': result}))
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'error': str(exc)}))
        sys.exit(1)
