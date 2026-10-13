from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from installer import patch_native_mail as native_mail
from installer import patch_native_navigation as transaction


def fingerprint(value):
    return len(value), hashlib.sha256(value).hexdigest()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / 'app' / '0.0.9.627'
    specs, files = {}, {}
    for name, relative in [('studio_ui', 'plugins/Studio/NutStudioUI.dll'),
                           ('container_plugin', 'plugins/Container/NutContainerPlugin.dll')]:
        original = name.encode() + b'-before-tail'
        patched = original.replace(b'before', b'after!')
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
        files[path] = original, patched
        specs[name] = {'relative_path': relative,
                       'fingerprints': {'original': fingerprint(original), 'patched': fingerprint(patched)},
                       'binary_replacements': ((name, b'-before-tail', 1, b'after!'),)}
    frontend = root / 'resources' / 'feapp.dat'
    frontend.parent.mkdir()
    frontend.write_bytes(b'current frontend is deliberately not a registered archive')
    monkeypatch.setattr(native_mail, '_SPECS', specs)
    return tmp_path, root, files, frontend


def test_native_mail_without_private_manifest_preserves_frontend_and_is_idempotent(installed):
    work, root, files, frontend = installed
    sentinel = frontend.read_bytes()
    result = native_mail.patch_native_mail(root, work_root=work)
    assert result['status'] == 'PATCHED'
    assert set(result['files']) == {'studio_ui', 'container_plugin'}
    for path, (original, patched) in files.items():
        assert path.read_bytes() == patched
        assert path.with_name(path.name + '.native-nav.orig').read_bytes() == original
    assert native_mail.patch_native_mail(root, work_root=work)['status'] == 'ALREADY_PATCHED'
    assert all(path.read_bytes() == patched for path, (_, patched) in files.items())
    assert frontend.read_bytes() == sentinel
    assert not frontend.with_name(frontend.name + '.native-nav.orig').exists()


def test_native_mail_unknown_second_dll_changes_neither(installed):
    work, root, files, _ = installed
    first, second = files
    second.write_bytes(b'unknown build')
    with pytest.raises(transaction.NativeNavigationPatchError, match='NATIVE_NAV_UNSUPPORTED_INPUT'):
        native_mail.patch_native_mail(root, work_root=work)
    assert first.read_bytes() == files[first][0]
    assert not any(p.with_name(p.name + '.native-nav.orig').exists() for p in files)


def test_native_mail_resumes_partial_backup_or_interrupted_publish(installed):
    work, root, files, _ = installed
    first = next(iter(files))
    first.with_name(first.name + '.native-nav.orig').write_bytes(files[first][0])
    assert native_mail.patch_native_mail(root, work_root=work)['status'] == 'PATCHED'
    first.write_bytes(files[first][0])
    assert native_mail.patch_native_mail(root, work_root=work)['status'] == 'PATCHED'
    assert all(path.read_bytes() == patched for path, (_, patched) in files.items())


def test_native_mail_rejects_unsafe_partial_recovery(installed):
    work, root, files, _ = installed
    first = next(iter(files))
    first.with_name(first.name + '.native-nav.orig').write_bytes(files[first][0])
    first.write_bytes(files[first][1])
    with pytest.raises(transaction.NativeNavigationPatchError, match='NATIVE_NAV_RECOVERY_UNSAFE'):
        native_mail.patch_native_mail(root, work_root=work)


def test_native_mail_rejects_tampered_backup(installed):
    work, root, files, _ = installed
    native_mail.patch_native_mail(root, work_root=work)
    first = next(iter(files))
    first.with_name(first.name + '.native-nav.orig').write_bytes(b'tampered')
    with pytest.raises(transaction.NativeNavigationPatchError, match='NATIVE_NAV_BACKUP_TAMPERED'):
        native_mail.patch_native_mail(root, work_root=work)


def test_native_mail_rollback_when_second_publish_fails(installed, monkeypatch):
    work, root, files, _ = installed
    first, second = files
    replace = transaction.os.replace
    def fail_second(source, target):
        if Path(target) == second:
            raise PermissionError('synthetic file lock')
        return replace(source, target)
    monkeypatch.setattr(transaction.os, 'replace', fail_second)
    with pytest.raises(transaction.NativeNavigationPatchError, match='NATIVE_NAV_PUBLICATION_FAILED'):
        native_mail.patch_native_mail(root, work_root=work)
    assert all(path.read_bytes() == original for path, (original, _) in files.items())
    assert not any(p.with_name(p.name + '.native-nav.orig').exists() for p in files)


def test_native_mail_requires_managed_version_root(installed):
    work, root, _, _ = installed
    with pytest.raises(transaction.NativeNavigationPatchError, match='NATIVE_NAV_UNMANAGED_ROOT'):
        native_mail.patch_native_mail(root)
    with pytest.raises(transaction.NativeNavigationPatchError, match='NATIVE_NAV_UNMANAGED_ROOT'):
        native_mail.patch_native_mail(root, work_root=work.parent)


def test_native_mail_rejects_reparse_path(installed, monkeypatch):
    work, root, files, _ = installed
    first = next(iter(files))
    monkeypatch.setattr(transaction, '_is_reparse_point', lambda path: path == first.parent)
    with pytest.raises(transaction.NativeNavigationPatchError, match='NATIVE_NAV_UNSAFE_PATH'):
        native_mail.patch_native_mail(root, work_root=work)
