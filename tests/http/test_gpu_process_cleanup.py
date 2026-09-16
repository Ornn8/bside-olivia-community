import os
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name == 'nt', reason='POSIX model workers use separate sessions')
def test_cancellation_kills_model_in_its_own_session(tmp_path):
    import psutil
    from gpu_service.server import kill_posix_tree
    marker = tmp_path / 'child.pid'
    code = "import subprocess,sys,time; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],start_new_session=True); Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(120)"
    parent = subprocess.Popen([sys.executable, '-c', code, str(marker)], start_new_session=True)
    child_pid = None
    def child_alive():
        try: return psutil.Process(child_pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess: return False
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        child_pid = int(marker.read_text())
        assert os.getpgid(child_pid) != os.getpgid(parent.pid)
        kill_posix_tree(parent.pid)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while child_alive() and time.monotonic() < deadline:
            time.sleep(.05)
        assert not child_alive()
    finally:
        kill_posix_tree(parent.pid)
        if child_pid and psutil.pid_exists(child_pid):
            try: psutil.Process(child_pid).kill()
            except psutil.NoSuchProcess: pass
