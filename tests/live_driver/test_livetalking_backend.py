from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from runtime.visual.livetalking import LIVE_TALKING_REVISION, LiveTalkingConfig
from runtime.visual.livetalking_backend import LiveTalkingVisualBackend
from visual_driver import (
    DRIVEN,
    FALLBACK,
    OriginalVisualFrame,
    VisualDriver,
    VisualDriverRequest,
    VisualDriverResult,
)


ASSET_REF = "asset_4d1c44521d987dde8e6bd6bf0b0fd4f5"


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "manifest_kind": "private_asset_manifest",
        "tool_version": "1",
        "roots": [{"alias": "fixture", "item_count": 1}],
        "items": [{
            "logical_id": ASSET_REF,
            "root_alias": "fixture",
            "relative_path": "live.mp4",
            "extension": ".mp4",
            "category": "video",
            "bytes": 1,
            "sha256": "b" * 64,
            "media_metadata": {"image": None, "video": None, "audio": None},
            "probe_status": "unavailable",
            "reason": "synthetic_fixture",
        }],
    }


def _request() -> VisualDriverRequest:
    return VisualDriverRequest(
        original=OriginalVisualFrame(
            state_id="live",
            asset_ref=ASSET_REF,
            frame=np.zeros((8, 8, 3), dtype=np.uint8),
            asset_manifest=_manifest(),
        ),
        speaking_mask=np.ones((8, 8), dtype=np.uint8),
        turn_id="turn-1",
        chunk_id="0:4",
        audio_pcm16=b"\x01\x00" * 320,
        sample_rate=16_000,
        sample_count=320,
        audio_start_seconds=0.08,
        audio_end_seconds=0.10,
        pts_seconds=0.08,
    )


def test_backend_maps_chunk_pts_to_fixed_livetalking_index_and_cleans_temporary_media(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def fake_capture(
        _config,
        *,
        audio_path,
        output_dir,
        frame_indices,
        worker_path,
        cancel_event,
        process_callback,
    ):
        assert not cancel_event.is_set()
        process_callback(None)
        with wave.open(str(audio_path), "rb") as stream:
            observed["wav"] = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes())
        observed["indices"] = tuple(frame_indices)
        observed["worker"] = worker_path
        candidate = Path(output_dir) / "frame_0002.png"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"fake-upstream-png")
        return [candidate]

    backend = LiveTalkingVisualBackend(
        config=object(),
        evidence_root=tmp_path / "b10b-data" / ".evidence",
        capture=fake_capture,
        image_reader=lambda _path: np.full((8, 8, 3), 255, dtype=np.uint8),
    )

    result = VisualDriver(backend).render(_request())

    assert result.status == DRIVEN
    assert observed["wav"] == (1, 2, 16_000, 1600)
    assert observed["indices"] == (2,)
    assert list((tmp_path / "b10b-data" / ".evidence").iterdir()) == []


def test_backend_missing_timed_pcm_falls_back_to_the_original_frame(tmp_path: Path) -> None:
    backend = LiveTalkingVisualBackend(config=object(), evidence_root=tmp_path / ".evidence")
    request = VisualDriverRequest(
        original=_request().original,
        speaking_mask=np.ones((8, 8), dtype=np.uint8),
    )

    result = VisualDriver(backend).render(request)

    assert result.status == FALLBACK
    assert result.fallback_reason == "visual_audio_timing_missing"


@pytest.mark.parametrize(
    ("stop_method", "expected_reason"),
    [("release_turn", "livetalking_turn_released"), ("close", "livetalking_backend_closed")],
)
def test_stop_before_delayed_render_rejects_without_starting_capture_or_creating_media(
    tmp_path: Path,
    stop_method: str,
    expected_reason: str,
) -> None:
    capture_calls = 0
    render_ready = threading.Event()
    enter_render = threading.Event()
    result: dict[str, VisualDriverResult] = {}

    def forbidden_capture(*args, **kwargs):
        nonlocal capture_calls
        capture_calls += 1
        raise AssertionError("capture must not start after stop")

    evidence = tmp_path / ".evidence"
    backend = LiveTalkingVisualBackend(
        config=object(),
        evidence_root=evidence,
        capture=forbidden_capture,
    )

    def delayed_render() -> None:
        render_ready.set()
        assert enter_render.wait(timeout=0.5)
        result["value"] = VisualDriver(backend).render(_request())

    render = threading.Thread(target=delayed_render, daemon=True)
    render.start()
    assert render_ready.wait(timeout=0.2)
    for _ in range(2):
        if stop_method == "release_turn":
            backend.release_turn("turn-1")
        else:
            backend.close()
    enter_render.set()
    render.join(timeout=0.5)

    assert not render.is_alive()
    assert result["value"].status == FALLBACK
    assert result["value"].fallback_reason == expected_reason
    assert capture_calls == 0
    assert not evidence.exists()


def _official_runtime_config(tmp_path: Path) -> LiveTalkingConfig:
    runtime = tmp_path / "LiveTalking"
    avatar = runtime / "data" / "avatars" / "b11_olivia"
    for frames in (avatar / "full_imgs", avatar / "face_imgs"):
        frames.mkdir(parents=True)
        (frames / "00000000.png").write_bytes(b"frame")
    (avatar / "coords.pkl").write_bytes(b"coords")
    checkpoint = tmp_path / "models" / "wav2lip256.pth"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"checkpoint")
    original = tmp_path / "original.png"
    original.write_bytes(b"original")
    work = tmp_path / "work"
    work.mkdir()
    python = tmp_path / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    return LiveTalkingConfig(
        runtime_root=runtime,
        python_executable=python,
        checkpoint_path=checkpoint,
        checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        checkpoint_url="https://github.com/lipku/LiveTalking",
        checkpoint_revision="wav2lip256.pth",
        checkpoint_license="Wav2Lip upstream terms; see provenance",
        avatar_payload=avatar,
        avatar_id="b11_olivia",
        original_reference=original,
        work_root=work,
        upstream_revision=LIVE_TALKING_REVISION,
    )


@pytest.mark.parametrize("stop_method", ["release_turn", "close"])
def test_stop_terminates_blocking_worker_and_cleans_temporary_media(
    tmp_path: Path,
    stop_method: str,
) -> None:
    started = threading.Event()

    class BlockingWorker:
        def __init__(self) -> None:
            self.active = True

        def terminate(self) -> None:
            self.active = False

        def kill(self) -> None:
            self.active = False

        def wait(self, timeout=None) -> int:
            self.active = False
            return -15

    worker = BlockingWorker()

    def blocking_capture(
        _config,
        *,
        audio_path,
        output_dir,
        frame_indices,
        worker_path,
        cancel_event,
        process_callback,
    ):
        del audio_path, output_dir, frame_indices, worker_path
        process_callback(worker)
        started.set()
        try:
            if not cancel_event.wait(timeout=1.0):
                raise RuntimeError("fake worker was not cancelled")
            raise RuntimeError("fake worker cancelled")
        finally:
            process_callback(None)

    evidence = tmp_path / ".evidence"
    backend = LiveTalkingVisualBackend(
        config=object(),
        evidence_root=evidence,
        capture=blocking_capture,
    )
    render = threading.Thread(target=lambda: VisualDriver(backend).render(_request()), daemon=True)
    render.start()

    assert started.wait(timeout=0.2)
    if stop_method == "release_turn":
        backend.release_turn("turn-1")
    else:
        backend.close()
    render.join(timeout=0.5)

    assert not render.is_alive()
    assert worker.active is False
    assert list(evidence.iterdir()) == []


@pytest.mark.parametrize("stop_method", ["release_turn", "close"])
def test_stop_cancels_official_capture_during_dependency_preflight_and_cleans_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_method: str,
) -> None:
    preflight_started = threading.Event()
    real_popen = subprocess.Popen
    process: dict[str, subprocess.Popen] = {}

    def blocking_popen(*_args, **_kwargs):
        process["value"] = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        preflight_started.set()
        return process["value"]

    monkeypatch.setattr("runtime.visual.livetalking.subprocess.Popen", blocking_popen)
    evidence = tmp_path / ".evidence"
    backend = LiveTalkingVisualBackend(
        config=_official_runtime_config(tmp_path),
        evidence_root=evidence,
    )
    render = threading.Thread(target=lambda: VisualDriver(backend).render(_request()), daemon=True)
    render.start()
    assert preflight_started.wait(timeout=0.5)

    started = time.monotonic()
    if stop_method == "release_turn":
        backend.release_turn("turn-1")
    else:
        backend.close()
    elapsed = time.monotonic() - started
    alive_at_return = render.is_alive()
    media_at_return = list(evidence.iterdir()) if evidence.exists() else []
    process_active_at_return = process["value"].poll() is None
    if process_active_at_return:
        process["value"].terminate()
        process["value"].wait(timeout=1.0)
    render.join(timeout=0.5)

    assert elapsed < 0.5
    assert alive_at_return is False
    assert process_active_at_return is False
    assert media_at_return == []
