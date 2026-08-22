"""Operational CLI for the B11 external LiveTalking adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.visual.livetalking import LiveTalkingConfig, capture_candidate_frames, runtime_health


_RESTORE_SOURCE_FRAMES_SCRIPT = r"""
import cv2
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[2])
output.mkdir(parents=True, exist_ok=True)
capture = cv2.VideoCapture(str(source))
if not capture.isOpened():
    raise SystemExit("ORIGINAL_SOURCE_OPEN_FAILED")
count = 0
while True:
    ok, frame = capture.read()
    if not ok:
        break
    target = output / f"{count:08d}.png"
    if not cv2.imwrite(str(target), frame):
        capture.release()
        raise SystemExit("ORIGINAL_FRAME_WRITE_FAILED")
    count += 1
capture.release()
if count == 0:
    raise SystemExit("ORIGINAL_SOURCE_EMPTY")
print(count)
""".strip()


def _read_config(path: Path) -> LiveTalkingConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    settings = raw.get("settings", raw) if isinstance(raw, dict) else {}
    if not isinstance(settings, dict):
        raise ValueError("configuration settings must be an object")
    return LiveTalkingConfig(**settings)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_source_frame_restore_command(config: LiveTalkingConfig, source: Path) -> list[str]:
    """Copy exact decoded source frames into the pinned upstream full_imgs storage.

    LiveTalking's pinned genavatar adds a watermark while extracting full
    frames.  This subprocess only restores the original decoded video frames;
    face detection, coordinates, model inference and paste-back remain the
    pinned upstream operations.
    """

    return [
        str(config.python()),
        "-c",
        _RESTORE_SOURCE_FRAMES_SCRIPT,
        str(source),
        str(config.avatar_payload / "full_imgs"),
    ]


def _restore_original_full_frames(
    config: LiveTalkingConfig,
    source: Path,
    *,
    timeout: int,
) -> dict[str, Any]:
    command = _build_source_frame_restore_command(config, source)
    try:
        result = subprocess.run(
            command,
            cwd=str(config.runtime_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "UNAVAILABLE", "reason": type(exc).__name__, "network_called": False}
    if result.returncode != 0:
        return {
            "status": "UNAVAILABLE",
            "reason": "ORIGINAL_FRAME_RESTORE_FAILED",
            "returncode": result.returncode,
            "stderr_tail": result.stderr[-2000:],
            "network_called": False,
        }
    try:
        frame_count = int(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"status": "UNAVAILABLE", "reason": "ORIGINAL_FRAME_COUNT_INVALID", "network_called": False}
    return {
        "status": "HEALTHY" if frame_count > 0 else "UNAVAILABLE",
        "frame_count": frame_count,
        "source": str(source),
        "output": str(config.avatar_payload / "full_imgs"),
        "network_called": False,
    }


def _prepare_avatar(args: argparse.Namespace, config: LiveTalkingConfig) -> dict[str, Any]:
    source = Path(args.source).expanduser()
    if not source.is_file():
        return {"status": "UNAVAILABLE", "reason": "ORIGINAL_SOURCE_MISSING"}
    s3fd = config.runtime_root / "avatars" / "wav2lip" / "face_detection" / "detection" / "sfd" / "s3fd.pth"
    if not s3fd.is_file():
        return {
            "status": "UNAVAILABLE",
            "reason": "S3FD_CHECKPOINT_MISSING",
            "s3fd_path": str(s3fd),
            "network_called": False,
        }
    save_path = config.avatar_payload.parent
    command = [
        str(config.python()),
        "-m",
        "avatars.wav2lip.genavatar",
        "--video_path",
        str(source),
        "--avatar_id",
        config.avatar_id,
        "--save_path",
        str(save_path),
        "--img_size",
        "256",
        "--pads",
        "0",
        "10",
        "0",
        "0",
        "--face_det_batch_size",
        str(args.face_det_batch_size),
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(config.runtime_root)
    result = subprocess.run(
        command,
        cwd=str(config.work_root),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=args.timeout,
    )
    if result.returncode != 0:
        return {
            "status": "UNAVAILABLE",
            "returncode": result.returncode,
            "avatar_payload": str(config.avatar_payload),
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-2000:],
            "network_called": False,
        }
    restored = _restore_original_full_frames(config, source, timeout=args.timeout)
    return {
        "status": restored["status"],
        "returncode": result.returncode,
        "avatar_payload": str(config.avatar_payload),
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
        "original_full_frames": restored,
        "network_called": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B11 LiveTalking external runtime operations")
    parser.add_argument("--config", type=Path, required=True, help="B10B config.json or settings JSON")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("health")
    prepare = subparsers.add_parser("prepare-avatar")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--face-det-batch-size", type=int, default=1)
    prepare.add_argument("--timeout", type=int, default=1800)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--audio", type=Path, required=True)
    capture.add_argument("--output-dir", type=Path, required=True)
    capture.add_argument("--worker", type=Path, default=Path(__file__).with_name("livetalking_worker.py"))
    capture.add_argument("--frame-indices", required=True)
    args = parser.parse_args(argv)

    try:
        config = _read_config(args.config)
        if args.operation == "health":
            result = runtime_health(config)
        elif args.operation == "prepare-avatar":
            result = _prepare_avatar(args, config)
        else:
            indices = tuple(int(item) for item in args.frame_indices.split(",") if item.strip())
            frames = capture_candidate_frames(
                config,
                audio_path=args.audio,
                output_dir=args.output_dir,
                frame_indices=indices,
                worker_path=args.worker.resolve(),
            )
            result = {"status": "HEALTHY", "frames": [{"path": str(frame), "sha256": _digest(frame)} for frame in frames]}
    except Exception as exc:
        result = {"status": "UNAVAILABLE", "reason": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "HEALTHY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
