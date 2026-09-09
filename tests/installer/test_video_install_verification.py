import hashlib
import threading
import zipfile

import pytest

from installer import component_update
from video_capability_install import _extract_zip_safely


def test_deferred_extraction_still_detects_same_size_disk_corruption(tmp_path, monkeypatch):
    archive = tmp_path / 'runtime.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('worker.py', b'good')
    root = tmp_path / 'output'
    original_open = type(root).open
    reads = []

    def track_open(path, mode='r', *args, **kwargs):
        if path == root / 'worker.py' and mode == 'rb':
            reads.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(type(root), 'open', track_open)
    entries = _extract_zip_safely(archive, root, strip_components=0, verify_written=False)
    assert reads == []
    (root / 'worker.py').write_bytes(b'evil')
    with pytest.raises(component_update.ComponentUpdateError, match='UPDATE_STAGED_TREE_MISMATCH'):
        component_update._verify_staged_tree(root, entries, workers=4)
    assert reads


def test_parallel_verification_is_bounded_and_reports_monotonic_progress(tmp_path, monkeypatch):
    entries = []
    for index in range(12):
        content = str(index).encode()
        name = f'{index}.bin'
        (tmp_path / name).write_bytes(content)
        entries.append({'path': name, 'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    original = component_update._file_sha256
    barrier = threading.Barrier(4, timeout=5)
    active = maximum = 0
    lock = threading.Lock()

    def read(path):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            barrier.wait()
            return original(path)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(component_update, '_file_sha256', read)
    progress = []
    component_update._verify_staged_tree(tmp_path, entries, workers=4,
        progress=lambda done, total: progress.append((done, total)))
    assert maximum == 4
    assert progress == sorted(progress)
    assert progress[-1] == (14, 14)
    (tmp_path / 'unexpected').write_bytes(b'')
    with pytest.raises(component_update.ComponentUpdateError):
        component_update._verify_staged_tree(tmp_path, entries, workers=4)
