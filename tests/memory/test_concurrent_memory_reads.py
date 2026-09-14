from concurrent.futures import ThreadPoolExecutor
import threading

from tests.memory.test_mem0_memory import FakeMem0, _config
from runtime.memory.mem0_memory import Mem0ConversationMemoryAdapter


def test_status_during_normal_list_read_does_not_report_unavailable(tmp_path):
    entered, release, status_started = threading.Event(), threading.Event(), threading.Event()
    class SlowRead(FakeMem0):
        def get_all(self, **kwargs):
            entered.set()
            assert release.wait(2)
            return super().get_all(**kwargs)
    memory = Mem0ConversationMemoryAdapter(SlowRead(), _config(tmp_path))
    def status():
        status_started.set()
        return memory.status()
    with ThreadPoolExecutor(max_workers=2) as pool:
        listed = pool.submit(memory.list_memories, user_id='local-user', limit=50)
        assert entered.wait(1)
        checked = pool.submit(status)
        assert status_started.wait(1)
        # Give the concurrent status call a chance to hit the active read.
        try:
            from concurrent.futures import TimeoutError
            try:
                result = checked.result(timeout=0.05)
            except TimeoutError:
                result = None
        finally:
            release.set()
        assert listed.result() == ()
        assert (result or checked.result()).status == 'available'
