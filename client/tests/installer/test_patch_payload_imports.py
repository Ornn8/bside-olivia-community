"""Every runtime module that shipped code imports must be inside the update patch."""
import pathlib
import re

from installer import full_patch


def _shipped(relative):
    directories = tuple(d.rstrip('/') + '/' for d in (*full_patch.PAYLOAD_DIRS, *full_patch.PAYLOAD_EXTRA_DIRS))
    return relative in full_patch.PAYLOAD_EXTRA_FILES or relative.startswith(directories) or '/' not in relative


def test_imported_runtime_modules_are_in_the_patch_payload():
    missing = set()
    for path in pathlib.Path('.').rglob('*.py'):
        relative = path.as_posix()
        if relative.startswith(('tests/', '.')) or not _shipped(relative):
            continue
        source = path.read_text(encoding='utf-8', errors='ignore')
        for module in re.findall(r'(?:from|import)\s+(runtime(?:\.\w+)+)', source):
            parts = module.split('.')
            for size in range(len(parts), 1, -1):
                candidate = '/'.join(parts[:size]) + '.py'
                if pathlib.Path(candidate).exists():
                    if not _shipped(candidate):
                        missing.add(f'{candidate} (imported by {relative})')
                    break
    assert not missing, 'add to installer/full_patch.py PAYLOAD_EXTRA_FILES: ' + ', '.join(sorted(missing))
