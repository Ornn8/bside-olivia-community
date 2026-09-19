"""Delegate B11 candidate-frame capture to the fixed LiveTalking Wav2Lip code.

The worker contains no renderer or model implementation.  It imports the
external upstream's loader, mel extractor, batch inference and paste-back
methods, and writes only the requested PNG evidence frames to the external
output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixed_revision(runtime_root: Path, expected: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(runtime_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip().lower() != expected.lower():
        raise RuntimeError("LIVE_TALKING_REVISION_MISMATCH")


def _indices(value: str) -> tuple[int, ...]:
    parsed = tuple(int(item) for item in value.split(",") if item.strip())
    if not parsed or any(item < 0 for item in parsed):
        raise ValueError("frame indices must be non-negative")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B11 LiveTalking official delegation worker")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--checkpoint-sha256", required=True)
    parser.add_argument("--avatar-payload", type=Path, required=True)
    parser.add_argument("--avatar-id", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--original-reference", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--frame-indices", required=True)
    parser.add_argument("--upstream-revision", required=True)
    args = parser.parse_args(argv)

    if _sha256(args.checkpoint).lower() != args.checkpoint_sha256.lower():
        raise RuntimeError("CHECKPOINT_HASH_MISMATCH")
    if not args.avatar_payload.is_dir() or not args.original_reference.is_file() or not args.audio.is_file():
        raise RuntimeError("EXTERNAL_INPUT_MISSING")
    _fixed_revision(args.runtime_root, args.upstream_revision)

    args.work_root.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.runtime_root))
    # The fixed upstream logger opens ``livetalking.log`` relative to the
    # process CWD. Import it from the writable evidence root first, then move
    # to the upstream root so its hard-coded avatar loader still resolves
    # ``./data/avatars/<avatar_id>`` without copying the payload.
    os.chdir(args.work_root)
    import utils.logger  # noqa: F401

    os.chdir(args.runtime_root)
    import cv2
    import numpy as np

    from avatars.wav2lip import audio as official_audio
    from avatars.wav2lip_avatar import LipReal, load_avatar, load_model

    model = load_model(str(args.checkpoint))
    frame_cycle, face_cycle, coord_cycle = load_avatar(args.avatar_id)
    mel = official_audio.melspectrogram(official_audio.load_wav(str(args.audio), 16000))
    if len(frame_cycle) == 0 or mel.shape[1] < 1:
        raise RuntimeError("OFFICIAL_INPUT_EMPTY")

    # LipReal's methods are the pinned upstream batch inference and paste-back path.
    # The small namespace supplies only the state those methods already use;
    # BaseAvatar construction would start unrelated TTS/transport services.
    class OfficialBatchState:
        batch_size = 1

    state = OfficialBatchState()
    state.model = model
    state.face_list_cycle = face_cycle
    state.frame_list_cycle = frame_cycle
    state.coord_list_cycle = coord_cycle
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for index in _indices(args.frame_indices):
        start = min(int(index * 80.0 / 25.0), max(0, mel.shape[1] - 16))
        mel_chunk = mel[:, start : start + 16]
        if mel_chunk.shape[1] < 16:
            raise RuntimeError("AUDIO_MEL_TOO_SHORT")
        predicted = LipReal.inference_batch(state, index, np.asarray([mel_chunk]))[0]
        frame = LipReal.paste_back_frame(state, predicted, index % len(frame_cycle))
        # ``paste_back_frame`` returns the upstream full frame with only the
        # upstream face crop replaced; no project-side pixels are synthesized.
        target = args.output_dir / f"frame_{index:04d}.png"
        if not cv2.imwrite(str(target), frame):
            raise RuntimeError("FRAME_WRITE_FAILED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
