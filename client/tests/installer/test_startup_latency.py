from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess
import sys
import time

import pytest

import installer.start_local as launcher


@pytest.mark.skipif(os.name != 'nt', reason='Windows Script Host is required')
def test_hidden_launcher_error_remains_visible_in_batch_mode(tmp_path):
    import ctypes
    from ctypes import wintypes
    template = Path(launcher.__file__).with_name('start_hidden.vbs.txt')
    script = tmp_path / 'START.vbs'
    title = f'Olivia startup test {tmp_path.name}'
    script.write_text(template.read_text(encoding='utf-8').replace('Olivia 本地版', title), encoding='utf-16')
    (tmp_path / 'START.cmd').write_text('@echo off\nexit /b 2\n', encoding='utf-8')
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    process = subprocess.Popen([str(Path(os.environ['WINDIR']) / 'System32/wscript.exe'),
        '//B', '//Nologo', str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
    visible = []
    @callback_type
    def find(window, _):
        caption = ctypes.create_unicode_buffer(200)
        user32.GetWindowTextW(window, caption, len(caption))
        if caption.value == title and user32.IsWindowVisible(window):
            visible.append(window)
        return True
    try:
        # The separate interactive WScript host can start slowly on a busy runner.
        deadline = time.monotonic() + 15
        while not visible and process.poll() is None and time.monotonic() < deadline:
            user32.EnumWindows(find, 0)
            time.sleep(0.05)
        assert visible, 'The hidden shortcut swallowed the launch failure dialog'
        for window in visible:
            user32.PostMessageW(window, 0x0010, 0, 0)
        assert process.wait(timeout=3) == 2
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


def test_unexpected_launch_exception_records_a_safe_failure(tmp_path, monkeypatch, capsys):
    from installer import version_launcher
    args = SimpleNamespace(action='start', install_root=tmp_path, arguments=[])
    monkeypatch.setattr(version_launcher, '_entrypoint_arguments',
        lambda *a: (_ for _ in ()).throw(ValueError('private-user-path-and-key')))
    assert version_launcher._run_action(args) == 2
    output = capsys.readouterr().out
    assert 'START_ACTION_FAILED' in output
    assert 'private-user' not in output
    events = (tmp_path / 'data/logs/launcher.jsonl').read_text()
    assert 'private-user' not in events
    assert json.loads(events)['exception_type'] == 'ValueError'


def test_backend_import_failure_is_recorded_without_the_raw_traceback(tmp_path):
    entrypoint = tmp_path / 'private-user-server.py'
    entrypoint.write_text('import nonexistent_olivia_startup_dependency\n', encoding='utf-8')
    environment = os.environ.copy()
    environment['OLIVIA_LOCAL_DATA_ROOT'] = str(tmp_path / 'data')
    result = subprocess.run([sys.executable, '-c', launcher._BACKEND_BOOTSTRAP,
        str(Path(launcher.__file__).resolve().parents[1]), str(entrypoint)],
        env=environment, capture_output=True, timeout=5)
    assert result.returncode != 0
    log = tmp_path / 'data/logs/launcher.jsonl'
    assert log.exists(), 'The backend discarded the reason it could not start'
    raw = log.read_text()
    event = json.loads(raw)
    assert event['exception_type'] == 'ModuleNotFoundError'
    assert event['missing_module'] == 'nonexistent_olivia_startup_dependency'
    assert 'private-user-server' not in raw


@pytest.mark.parametrize('result', [0, 2])
def test_animation_starts_before_backend_preparation_and_cleans_up(tmp_path, monkeypatch, result):
    backend = tmp_path / 'local_backend'
    (backend / 'installer/assets').mkdir(parents=True)
    (backend / 'installer/assets/startup.mp4').write_bytes(b'video')
    (backend / 'installer/startup_animation.ps1').write_text('# player')
    (backend / 'local_server.py').touch()
    (backend / 'original_client_server.py').touch()
    calls = []

    class Player:
        def poll(self): return None
        def terminate(self): calls.append('closed')
        def wait(self, timeout): return 0

    def spawn(command, **kwargs):
        calls.append('animation')
        assert '-LauncherProcessId' in command
        assert '-ClientProcessIdFile' in command
        return Player()

    def prepare(args, root, selected, entrypoint, data_root, *, startup_player=None):
        calls.append('preparation')
        assert selected == backend
        assert isinstance(startup_player, Player)
        signal = Path(launcher.os.environ['OLIVIA_STARTUP_CLIENT_PID_FILE'])
        signal.write_text('123')
        return result

    monkeypatch.setattr(launcher, '_active_backend', lambda: backend)
    monkeypatch.setattr(launcher.subprocess, 'Popen', spawn)
    monkeypatch.setattr(launcher, '_launch', prepare, raising=False)
    assert launcher.main(['--install-root', str(tmp_path)]) == result
    assert calls == ['animation', 'preparation', 'closed']
    assert 'OLIVIA_STARTUP_CLIENT_PID_FILE' not in launcher.os.environ
    assert not list((tmp_path / 'data/logs').glob('startup-client-*.pid'))


@pytest.mark.parametrize('attempt', [1, 2])
def test_early_animation_receives_native_pid_without_a_second_player(tmp_path, monkeypatch, attempt):
    from installer.native_window_layout import LayoutStatus
    signal = tmp_path / 'client.pid'
    starts = []
    def spawn(command, **kwargs):
        starts.append(command)
        return SimpleNamespace(pid=456, wait=lambda: 0)
    monkeypatch.setattr(launcher.subprocess, 'Popen', spawn)
    monkeypatch.setattr(launcher, 'guard_native_window_layout', lambda *a, **k: LayoutStatus.SKIPPED)
    assert launcher._run_client_with_native_layout(tmp_path / 'Olivia.exe', tmp_path,
        cwd=tmp_path, environment={'OLIVIA_STARTUP_CLIENT_PID_FILE': str(signal)},
        data_root=tmp_path, attempt=attempt) == 0
    assert len(starts) == 1
    assert signal.read_text() == '456'


def test_unwritable_pid_signal_closes_owned_animation_and_keeps_client_running(tmp_path, monkeypatch):
    from installer.native_window_layout import LayoutStatus
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('blocked')
    waits = []
    monkeypatch.setattr(launcher.subprocess, 'Popen',
        lambda *a, **k: SimpleNamespace(pid=789, wait=lambda: waits.append('client') or 0))
    monkeypatch.setattr(launcher, 'guard_native_window_layout', lambda *a, **k: LayoutStatus.SKIPPED)
    player = SimpleNamespace(terminate=lambda: waits.append('animation_closed'))
    assert launcher._run_client_with_native_layout(tmp_path / 'Olivia.exe', tmp_path,
        cwd=tmp_path, environment={'OLIVIA_STARTUP_CLIENT_PID_FILE': str(blocked / 'client.pid')},
        data_root=tmp_path, attempt=1, startup_player=player) == 0
    assert waits == ['animation_closed', 'client']


def test_unchanged_frontend_skips_repairs_but_archive_or_code_changes_invalidate(tmp_path, monkeypatch):
    client = tmp_path / 'app/Olivia.exe'
    resources = client.parent / 'resources'
    resources.mkdir(parents=True)
    (resources / 'feapp.dat').write_bytes(b'first')
    code_revision = ['revision-1']
    monkeypatch.setattr(launcher, '_client_executable', lambda _: client)
    monkeypatch.setattr(launcher, '_frontend_patcher_digest', lambda: code_revision[0], raising=False)
    repairs = []
    monkeypatch.setattr(launcher, 'repair_web_player_event_ids', lambda *a, **k: repairs.append('events'))
    monkeypatch.setattr(launcher, 'patch_companion_settings', lambda *a, **k: {'status': 'ALREADY_PATCHED'})
    monkeypatch.setattr('installer.patch_letter_stickers.patch_letter_stickers', lambda _: 'ALREADY_PATCHED')

    assert launcher._repair_client_frontend(tmp_path, 8899) == 'ALREADY_PATCHED'
    launcher._repair_client_frontend(tmp_path, 8899)
    assert repairs == ['events']
    (resources / 'feapp.dat').write_bytes(b'changed')
    launcher._repair_client_frontend(tmp_path, 8899)
    assert len(repairs) == 2
    code_revision[0] = 'revision-2'
    launcher._repair_client_frontend(tmp_path, 8899)
    assert len(repairs) == 3
    launcher._repair_client_frontend(tmp_path, 8900)
    assert len(repairs) == 4
    monkeypatch.setattr(launcher, '_frontend_patcher_digest',
        lambda: (_ for _ in ()).throw(OSError('optional receipt unavailable')))
    assert launcher._repair_client_frontend(tmp_path, 8900) == 'ALREADY_PATCHED'
    assert len(repairs) == 5


def test_failed_frontend_repair_does_not_create_success_receipt(tmp_path, monkeypatch):
    client = tmp_path / 'app/Olivia.exe'
    resources = client.parent / 'resources'
    resources.mkdir(parents=True)
    (resources / 'feapp.dat').write_bytes(b'first')
    monkeypatch.setattr(launcher, '_client_executable', lambda _: client)
    monkeypatch.setattr(launcher, 'repair_web_player_event_ids', lambda *a, **k: (_ for _ in ()).throw(ValueError('invalid')))
    with pytest.raises(ValueError):
        launcher._repair_client_frontend(tmp_path, 8899)
    assert not (tmp_path / 'data/logs/frontend-patch.json').exists()
