"""Thin adapter for the accepted LatentSync 1.5 video-reply route."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class LatentSyncReplyError(RuntimeError):
    """Stable product error for the external LatentSync process."""


def render_latentsync_video(
    source_video: Path,
    audio_path: Path,
    output_path: Path,
    *,
    python_path: Path,
    latentsync_root: Path,
    timeout_seconds: float = 21600.0,
) -> dict[str, object]:
    """Apply the accepted 1.5 settings to an original-motion source video."""

    source_video = Path(source_video)
    audio_path = Path(audio_path)
    output_path = Path(output_path)
    python_path = Path(python_path)
    latentsync_root = Path(latentsync_root)
    config_path = latentsync_root / "configs" / "unet" / "stage2.yaml"
    checkpoint_path = latentsync_root / "checkpoints" / "latentsync_unet.pt"
    required = (
        python_path,
        source_video,
        audio_path,
        latentsync_root / "scripts" / "inference.py",
        config_path,
        checkpoint_path,
    )
    if any(not path.is_file() for path in required):
        raise LatentSyncReplyError("LATENTSYNC_INPUT_UNAVAILABLE")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="olivia-latentsync-", dir=output_path.parent) as temporary:
        command = [
            str(python_path),
            "-m",
            "scripts.inference",
            "--unet_config_path",
            str(config_path),
            "--inference_ckpt_path",
            str(checkpoint_path),
            "--video_path",
            str(source_video),
            "--audio_path",
            str(audio_path),
            "--video_out_path",
            str(output_path),
            "--inference_steps",
            "20",
            "--guidance_scale",
            "1.5",
            "--temp_dir",
            temporary,
            "--seed",
            "1247",
            "--enable_deepcache",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=latentsync_root,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LatentSyncReplyError("LATENTSYNC_FAILED") from exc
    if result.returncode != 0:
        raise LatentSyncReplyError("LATENTSYNC_FAILED")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise LatentSyncReplyError("LATENTSYNC_OUTPUT_MISSING")
    return {
        "visual_provider": "LatentSync-1.5",
        "inference_steps": 20,
        "guidance_scale": 1.5,
        "deepcache": True,
        "scene_source": "official_motion_video",
    }


__all__ = ["LatentSyncReplyError", "render_latentsync_video"]
