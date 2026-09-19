import threading
import time

from runtime.media.background_video_readiness import BackgroundVideoReadiness


def wait_result(check, env):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = check(env)
        if not result.get("runtime_probe_pending"):
            return result
        time.sleep(0.005)
    raise AssertionError("background probe did not complete")


def test_slow_probe_returns_pending_and_runs_once():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe(env):
        calls.append(dict(env))
        entered.set()
        assert release.wait(2)
        return {"ready": True, "music_ready": True, "ordinary_missing_dependencies": []}

    check = BackgroundVideoReadiness(probe)
    try:
        before = time.monotonic()
        assert check({"root": "one"})["runtime_probe_pending"] is True
        assert time.monotonic() - before < 0.2
        assert entered.wait(1)
        for _ in range(10):
            assert check({"root": "one"})["ready"] is False
        assert len(calls) == 1
        release.set()
        assert wait_result(check, {"root": "one"})["ready"] is True
        assert len(calls) == 1
    finally:
        release.set()


def test_cache_invalidates_on_environment_and_manifest_content_not_rewrite(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("one")
    calls = []
    check = BackgroundVideoReadiness(
        lambda env: calls.append(dict(env)) or {"ready": True},
        fingerprint_paths=(manifest,),
    )
    assert wait_result(check, {"setting": "one"})["ready"]
    manifest.write_text("one")
    assert check({"setting": "one"})["ready"]
    assert len(calls) == 1
    manifest.write_text("two")
    assert check({"setting": "one"})["runtime_probe_pending"]
    assert wait_result(check, {"setting": "one"})["ready"]
    assert check({"setting": "two"})["runtime_probe_pending"]
    assert wait_result(check, {"setting": "two"})["ready"]
    assert len(calls) == 3


def test_failed_probe_is_cached_until_ttl_and_retried_without_blocking():
    now, calls = [0.0], []

    def probe(env):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("private diagnostic must not escape")
        return {"ready": True}

    check = BackgroundVideoReadiness(probe, ttl_seconds=30, clock=lambda: now[0])
    assert wait_result(check, {})["ready"] is False
    now[0] = 29
    assert check({})["ready"] is False
    assert len(calls) == 1
    now[0] = 31
    assert check({})["runtime_probe_pending"]
    assert wait_result(check, {})["ready"] is True
    assert len(calls) == 2


def test_bundle_install_marker_invalidates_missing_dependency_cache(tmp_path):
    marker = tmp_path / "capabilities/video/ordinary_video/.ready.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"version":"before-supplement"}')
    calls = []
    def probe(env):
        calls.append(1)
        return {"ready": "after-supplement" in marker.read_text()}
    check = BackgroundVideoReadiness(probe)
    env = {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    assert wait_result(check, env)["ready"] is False
    marker.write_text('{"version":"before-supplement"}')
    assert check(env)["ready"] is False
    assert len(calls) == 1
    marker.write_text('{"version":"after-supplement"}')
    assert check(env)["runtime_probe_pending"] is True
    assert wait_result(check, env)["ready"] is True
    assert len(calls) == 2
