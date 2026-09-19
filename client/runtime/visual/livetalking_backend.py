"""Thin B06-timed to B11 LiveTalking to B07 visual backend.

This adapter writes one temporary PCM16 WAV, delegates candidate-frame capture
to the fixed B11 worker, reads its single PNG into memory, and then removes the
temporary directory.  It owns no inference, model, or generated-media store.
"""

from __future__ import annotations

import tempfile
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from visual_driver import VisualDriverError, VisualDriverRequest

from .livetalking import LiveTalkingConfig, _stop_delegated_process, capture_candidate_frames


LIVE_TALKING_FRAME_RATE = 25.0
MAX_TURN_AUDIO_SECONDS = 120.0
WORKER_STOP_WAIT_SECONDS = 2.0


@dataclass
class _CaptureJob:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    process: Any | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def observe_process(self, process: Any | None) -> None:
        with self.lock:
            self.process = process
            cancelled = self.cancel_event.is_set()
        if process is not None and cancelled:
            _stop_delegated_process(process)

    def stop(self) -> None:
        self.cancel_event.set()
        with self.lock:
            process = self.process
        if process is not None:
            _stop_delegated_process(process)


def _default_image_reader(path: Path) -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise VisualDriverError("livetalking_frame_reader_unavailable", retryable=True) from exc
    frame = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if frame is None:
        raise VisualDriverError("livetalking_frame_read_failed")
    return frame


def frame_index_for_pts(pts_seconds: float) -> int:
    """Map a verified B06 audio PTS to the fixed upstream's 25fps index."""

    if not isinstance(pts_seconds, (int, float)) or isinstance(pts_seconds, bool) or pts_seconds < 0:
        raise VisualDriverError("visual_audio_timing_invalid")
    return int(float(pts_seconds) * LIVE_TALKING_FRAME_RATE)


class LiveTalkingVisualBackend:
    """B07 backend that delegates one timed PCM chunk to existing B11 capture."""

    def __init__(
        self,
        config: LiveTalkingConfig,
        *,
        evidence_root: Path | str,
        worker_path: Path | str | None = None,
        capture: Callable[..., Sequence[Path]] = capture_candidate_frames,
        image_reader: Callable[[Path], Any] = _default_image_reader,
    ) -> None:
        self._config = config
        self._evidence_root = Path(evidence_root).absolute()
        self._worker_path = (
            Path(worker_path).absolute()
            if worker_path is not None
            else Path(__file__).resolve().parents[2] / "tools" / "livetalking_worker.py"
        )
        self._capture = capture
        self._image_reader = image_reader
        self._turn_pcm: dict[str, tuple[int, bytearray]] = {}
        self._jobs: dict[str, _CaptureJob] = {}
        self._released_turns: set[str] = set()
        self._closed = False
        self._jobs_lock = threading.Lock()

    @staticmethod
    def _timed_pcm(request: VisualDriverRequest) -> tuple[bytes, int, int]:
        if (
            request.audio_pcm16 is None
            or request.sample_rate is None
            or request.sample_count is None
            or request.pts_seconds is None
        ):
            raise VisualDriverError("visual_audio_timing_missing")
        return request.audio_pcm16, request.sample_rate, frame_index_for_pts(request.pts_seconds)

    @staticmethod
    def _write_wav(path: Path, pcm16: bytes, sample_rate: int) -> None:
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes(pcm16)

    def _timeline_pcm(self, request: VisualDriverRequest, pcm16: bytes, sample_rate: int) -> bytes:
        if request.turn_id is None or request.audio_start_seconds is None:
            raise VisualDriverError("visual_audio_timing_missing")
        state = self._turn_pcm.get(request.turn_id)
        if state is None:
            leading_samples = round(request.audio_start_seconds * sample_rate)
            if abs(leading_samples / sample_rate - request.audio_start_seconds) > 1 / sample_rate:
                raise VisualDriverError("visual_audio_timing_invalid")
            timeline = bytearray(b"\x00\x00" * leading_samples)
        else:
            known_rate, existing = state
            if known_rate != sample_rate:
                raise VisualDriverError("visual_audio_sample_rate_changed")
            timeline = bytearray(existing)
            expected_start = len(timeline) / 2 / sample_rate
            if abs(expected_start - request.audio_start_seconds) > 1 / sample_rate:
                raise VisualDriverError("visual_audio_discontinuous")
        timeline.extend(pcm16)
        if len(timeline) / 2 / sample_rate > MAX_TURN_AUDIO_SECONDS:
            raise VisualDriverError("visual_audio_timeline_too_long")
        self._turn_pcm[request.turn_id] = (sample_rate, timeline)
        return bytes(timeline)

    def release_turn(self, turn_id: str) -> None:
        """Stop the turn's owned worker, then forget its temporary PCM."""

        with self._jobs_lock:
            self._released_turns.add(turn_id)
            self._turn_pcm.pop(turn_id, None)
            job = self._jobs.get(turn_id)
        if job is not None:
            job.stop()
            job.done_event.wait(timeout=WORKER_STOP_WAIT_SECONDS)

    def close(self) -> None:
        with self._jobs_lock:
            self._closed = True
            self._turn_pcm.clear()
            jobs = tuple(self._jobs.values())
        for job in jobs:
            job.stop()
        for job in jobs:
            job.done_event.wait(timeout=WORKER_STOP_WAIT_SECONDS)

    def render(self, request: VisualDriverRequest) -> Any:
        pcm16, sample_rate, frame_index = self._timed_pcm(request)
        if request.turn_id is None:
            raise VisualDriverError("visual_audio_timing_missing")
        job = _CaptureJob()
        with self._jobs_lock:
            if self._closed:
                raise VisualDriverError("livetalking_backend_closed")
            if request.turn_id in self._released_turns:
                raise VisualDriverError("livetalking_turn_released")
            existing = self._jobs.get(request.turn_id)
            timeline_pcm16 = self._timeline_pcm(request, pcm16, sample_rate)
            self._jobs[request.turn_id] = job
        try:
            if existing is not None:
                existing.stop()
            if job.cancel_event.is_set():
                raise VisualDriverError("livetalking_capture_cancelled", retryable=True)
            self._evidence_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="b06-b11-b07-", dir=self._evidence_root) as temporary:
                root = Path(temporary)
                audio_path = root / "chunk.wav"
                output_dir = root / "frames"
                self._write_wav(audio_path, timeline_pcm16, sample_rate)
                frames = self._capture(
                    self._config,
                    audio_path=audio_path,
                    output_dir=output_dir,
                    frame_indices=(frame_index,),
                    worker_path=self._worker_path,
                    cancel_event=job.cancel_event,
                    process_callback=job.observe_process,
                )
                if len(frames) != 1:
                    raise VisualDriverError("livetalking_frame_output_invalid")
                return self._image_reader(Path(frames[0]))
        except VisualDriverError:
            raise
        except Exception as exc:
            raise VisualDriverError("livetalking_capture_unavailable", retryable=True) from exc
        finally:
            job.done_event.set()
            with self._jobs_lock:
                if self._jobs.get(request.turn_id) is job:
                    self._jobs.pop(request.turn_id, None)


__all__ = ["LIVE_TALKING_FRAME_RATE", "LiveTalkingVisualBackend", "frame_index_for_pts"]
