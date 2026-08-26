"""B11 LiveTalking assembly contract: external-only, fail-closed, delegated."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.visual.livetalking import (
    LIVE_TALKING_REVISION,
    LiveTalkingConfig,
    LiveTalkingConfigError,
    LiveTalkingRuntimeError,
    build_worker_command,
    capture_candidate_frames,
    runtime_health,
)
from tools.livetalking_runtime import _build_source_frame_restore_command


def _config(tmp_path: Path, *, checkpoint_sha256: str | None = None) -> LiveTalkingConfig:
    runtime = tmp_path / "LiveTalking"
    runtime.mkdir()
    checkpoint = tmp_path / "models" / "wav2lip256.pth"
    checkpoint.parent.mkdir()
    avatar = runtime / "data" / "avatars" / "b11_olivia"
    avatar.mkdir(parents=True)
    (avatar / "full_imgs").mkdir()
    (avatar / "face_imgs").mkdir()
    (avatar / "full_imgs" / "00000000.png").write_bytes(b"official-full-frame")
    (avatar / "face_imgs" / "00000000.png").write_bytes(b"official-face-frame")
    (avatar / "coords.pkl").write_bytes(b"official-payload-marker")
    original = tmp_path / "original" / "reference.png"
    original.parent.mkdir()
    original.write_bytes(b"original-reference")
    work = tmp_path / "evidence" / "run"
    work.mkdir(parents=True)
    checkpoint.write_bytes(b"official-checkpoint")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    return LiveTalkingConfig(
        runtime_root=runtime,
        python_executable=tmp_path / "venv" / "Scripts" / "python.exe",
        checkpoint_path=checkpoint,
        checkpoint_sha256=checkpoint_sha256 or digest,
        checkpoint_url="https://github.com/lipku/LiveTalking",
        checkpoint_revision="wav2lip256.pth",
        checkpoint_license="Wav2Lip upstream terms; see provenance",
        avatar_payload=avatar,
        avatar_id="b11_olivia",
        original_reference=original,
        work_root=work,
        upstream_revision=LIVE_TALKING_REVISION,
    )


def _dependencies(_config: LiveTalkingConfig) -> dict[str, bool]:
    return {
        "aiohttp_cors": True,
        "aiortc": True,
        "torch": True,
        "cv2": True,
        "numpy": True,
        "soundfile": True,
        "librosa": True,
        "scipy": True,
        "resampy": True,
        "tqdm": True,
    }


def test_config_accepts_any_local_drive_and_rejects_unsafe_runtime_references(tmp_path: Path) -> None:
    config = _config(tmp_path)
    object.__setattr__(config, "runtime_root", Path("C:/models/LiveTalking"))
    object.__setattr__(config, "avatar_payload", Path("C:/models/LiveTalking/data/avatars/b11_olivia"))
    config.validate()

    for unsafe in (
        "relative/LiveTalking",
        "C:relative/LiveTalking",
        "C:/",
        "C:/models/../escape",
        r"\\server\share\LiveTalking",
        "https://example.invalid/LiveTalking",
    ):
        object.__setattr__(config, "runtime_root", Path(unsafe))
        object.__setattr__(config, "avatar_payload", Path(unsafe) / "data" / "avatars" / "b11_olivia")
        with pytest.raises(LiveTalkingConfigError, match="absolute local Windows"):
            config.validate()


def test_config_rejects_tilde_backslash_paths_before_expanduser() -> None:
    config = LiveTalkingConfig(
        runtime_root=Path(r"~\LiveTalking"),
        checkpoint_path=Path(r"~\models\wav2lip.pth"),
        checkpoint_sha256="a" * 64,
        avatar_payload=Path(r"~\LiveTalking\data\avatars\b11_olivia"),
        original_reference=Path(r"~\original.mp4"),
        work_root=Path(r"~\evidence"),
        checkpoint_url="https://example.invalid/checkpoint",
        checkpoint_revision="verified checkpoint",
        checkpoint_license="verified checkpoint license",
    )

    with pytest.raises(LiveTalkingConfigError, match="absolute local Windows"):
        config.validate()


@pytest.mark.parametrize(
    "unsafe",
    [
        r"C:\NUL",
        r"C:\CON\runtime",
        r"C:\provider\PRN.txt",
        r"C:\provider\AUX.",
        r"C:\provider\CLOCK$.log",
        r"C:\provider\COM1 ",
        r"C:\provider\LPT9.tar.gz",
    ],
)
def test_config_rejects_dos_device_name_segments(tmp_path: Path, unsafe: str) -> None:
    config = _config(tmp_path)
    object.__setattr__(config, "runtime_root", Path(unsafe))
    object.__setattr__(config, "avatar_payload", Path(unsafe) / "data" / "avatars" / "b11_olivia")

    with pytest.raises(LiveTalkingConfigError, match="absolute local Windows"):
        config.validate()


def test_health_is_fail_closed_when_checkpoint_or_payload_is_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.checkpoint_path.unlink()
    for child in config.avatar_payload.iterdir():
        if child.is_dir():
            for frame in child.iterdir():
                frame.unlink()
            child.rmdir()
        else:
            child.unlink()

    health = runtime_health(config, dependency_probe=_dependencies)

    assert health["status"] == "UNAVAILABLE"
    assert health["ready"] is False
    assert "CHECKPOINT_MISSING" in health["reason_codes"]
    assert "AVATAR_PAYLOAD_INCOMPLETE" in health["reason_codes"]
    assert health["external_assets_copied"] is False


def test_health_verifies_checkpoint_sha256_and_provenance(tmp_path: Path) -> None:
    config = _config(tmp_path, checkpoint_sha256="00" * 32)

    health = runtime_health(config, dependency_probe=_dependencies)

    assert health["status"] == "UNAVAILABLE"
    assert health["reason_codes"] == ["CHECKPOINT_HASH_MISMATCH"]
    assert health["provenance"]["upstream_revision"] == LIVE_TALKING_REVISION
    assert health["provenance"]["checkpoint_revision"] == "wav2lip256.pth"


def test_health_rejects_avatar_payload_without_real_frame_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.avatar_payload / "full_imgs" / "00000000.png").unlink()
    (config.avatar_payload / "face_imgs" / "00000000.png").unlink()

    health = runtime_health(config, dependency_probe=_dependencies)

    assert health["status"] == "UNAVAILABLE"
    assert "AVATAR_PAYLOAD_INCOMPLETE" in health["reason_codes"]


def test_worker_command_delegates_to_official_runtime_without_install_or_download(tmp_path: Path) -> None:
    config = _config(tmp_path)
    audio = tmp_path / "audio" / "sample.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    output = tmp_path / "evidence" / "frames"
    worker = tmp_path / "project" / "tools" / "livetalking_worker.py"
    worker.parent.mkdir(parents=True)

    command = build_worker_command(
        config,
        audio_path=audio,
        output_dir=output,
        frame_indices=(0, 2, 11),
        worker_path=worker,
    )
    encoded = " ".join(command).lower()

    assert command[0] == str(config.python_executable)
    assert str(worker) in command
    assert "--frame-indices" in command
    assert "0,2,11" in command
    assert "--download" not in encoded
    assert "--install" not in encoded
    assert LIVE_TALKING_REVISION in command


def test_original_frame_restore_is_a_source_copy_without_watermark_or_renderer(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "original" / "source.mp4"
    source.write_bytes(b"original-video")

    command = _build_source_frame_restore_command(config, source)

    assert command[0] == str(config.python_executable)
    assert command[1] == "-c"
    assert str(source) in command
    assert str(config.avatar_payload / "full_imgs") in command
    assert "VideoCapture" in command[2]
    assert "putText" not in command[2]


def test_capture_refuses_to_start_when_health_is_not_ready(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.checkpoint_path.unlink()
    called = False

    def runner(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(LiveTalkingRuntimeError, match="RUNTIME_NOT_READY"):
        capture_candidate_frames(
            config,
            audio_path=tmp_path / "audio.wav",
            output_dir=tmp_path / "frames",
            frame_indices=(0,),
            worker_path=tmp_path / "tools" / "livetalking_worker.py",
            dependency_probe=_dependencies,
            runner=runner,
        )
    assert called is False


def test_capture_rejects_missing_audio_before_delegate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    called = False

    def runner(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(LiveTalkingRuntimeError, match="AUDIO_MISSING"):
        capture_candidate_frames(
            config,
            audio_path=tmp_path / "missing.wav",
            output_dir=tmp_path / "frames",
            frame_indices=(0,),
            worker_path=tmp_path / "tools" / "livetalking_worker.py",
            dependency_probe=_dependencies,
            runner=runner,
        )
    assert called is False


def test_capture_returns_only_delegate_outputs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    output = tmp_path / "frames"
    worker = tmp_path / "tools" / "livetalking_worker.py"

    def runner(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        output.mkdir(exist_ok=True)
        for index in (0, 2):
            (output / f"frame_{index:04d}.png").write_bytes(f"frame-{index}".encode())
        return SimpleNamespace(returncode=0, stdout="delegate ok", stderr="")

    frames = capture_candidate_frames(
        config,
        audio_path=audio,
        output_dir=output,
        frame_indices=(0, 2),
        worker_path=worker,
        dependency_probe=_dependencies,
        runner=runner,
    )

    assert frames == [output / "frame_0000.png", output / "frame_0002.png"]


def test_capture_cancellation_terminates_the_owned_popen_worker(tmp_path: Path) -> None:
    config = _config(tmp_path)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    cancel = threading.Event()

    class BlockingProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout=None) -> int:
            assert timeout is not None
            return int(self.returncode or 0)

        def communicate(self, timeout=None):
            raise AssertionError("cancel must be observed before waiting on the worker")

    process = BlockingProcess()

    def observe(value) -> None:
        if value is process:
            cancel.set()

    with pytest.raises(LiveTalkingRuntimeError, match="DELEGATE_CANCELLED"):
        capture_candidate_frames(
            config,
            audio_path=audio,
            output_dir=tmp_path / "frames",
            frame_indices=(0,),
            worker_path=tmp_path / "tools" / "livetalking_worker.py",
            dependency_probe=_dependencies,
            cancel_event=cancel,
            process_callback=observe,
            process_factory=lambda *_args, **_kwargs: process,
        )

    assert process.terminated is True



def test_provenance_rejects_missing_fixed_upstream_and_checkpoint_metadata(tmp_path: Path) -> None:
    config = LiveTalkingConfig(
        runtime_root=tmp_path / "LiveTalking",
        checkpoint_path=tmp_path / "wav2lip256.pth",
        checkpoint_sha256="",
        avatar_payload=tmp_path / "LiveTalking" / "data" / "avatars" / "b11_olivia",
        original_reference=tmp_path / "original.mp4",
        work_root=tmp_path / "evidence",
        avatar_id="b11_olivia",
        checkpoint_url="",
        checkpoint_revision="",
        checkpoint_license="",
        upstream_source="",
        upstream_revision="",
        upstream_license="",
    )

    with pytest.raises(LiveTalkingConfigError):
        config.validate()
