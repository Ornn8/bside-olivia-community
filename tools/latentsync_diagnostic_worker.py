"""Run upstream inference unchanged, with a bounded private-data-free failure marker."""
from __future__ import annotations

import json
from pathlib import Path
import re
import runpy
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import argparse

MARKER = "OLIVIA_LATENTSYNC_FAILURE="


def install_path_safe_video_io(pipeline, *, temp_dir: str, output_path: str) -> None:
    """Replace the two upstream shell FFmpeg calls without editing model files."""
    from latentsync.utils import util

    def read_video(video_path, change_fps=True, use_decord=True):
        reader = util.read_video_decord if use_decord else util.read_video_cv2
        if change_fps:
            with tempfile.TemporaryDirectory(prefix="decode-", dir=temp_dir) as directory:
                converted = str(Path(directory) / "video.mp4")
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-nostdin",
                    "-i", str(video_path), "-r", "25", "-crf", "18", converted], check=True)
                frames = reader(converted)
        else:
            frames = reader(str(video_path))
        if not len(frames):
            raise RuntimeError("LATENTSYNC_VIDEO_DECODE_EMPTY")
        return frames

    def mux(command, *, shell=False):
        # This module has one subprocess call: the final audio/video mux.
        expected = f"ffmpeg -y -loglevel error -nostdin -i {str(Path(temp_dir) / 'video.mp4')} -i {str(Path(temp_dir) / 'audio.wav')} -c:v libx264 -crf 18 -c:a aac -q:v 0 -q:a 0 {output_path}"
        if command != expected or shell is not True:
            raise RuntimeError("LATENTSYNC_MUX_COMMAND_UNSUPPORTED")
        return subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin",
            "-i", str(Path(temp_dir) / "video.mp4"), "-i", str(Path(temp_dir) / "audio.wav"),
            "-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-q:v", "0", "-q:a", "0", output_path], check=True)

    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    pipeline.read_video = read_video
    pipeline.subprocess = SimpleNamespace(run=mux)


def project_exception(exc: Exception) -> dict[str, object]:
    result: dict[str, object] = {}
    name = type(exc).__name__
    if name in {"FileNotFoundError", "PermissionError", "RuntimeError", "OSError", "ValueError", "ImportError", "ModuleNotFoundError"}:
        result["exception_type"] = name
    frames = []
    traceback = exc.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__", "")
        if module == "__main__" and Path(frame.f_code.co_filename).as_posix().endswith("/scripts/inference.py"):
            module = "scripts.inference"
        if isinstance(module, str) and re.fullmatch(r"(?:latentsync|scripts|ffmpeg)(?:\.[A-Za-z_][A-Za-z_0-9]*)+|subprocess", module):
            frames.append({"module": module, "line": traceback.tb_lineno})
        if module == "subprocess" and isinstance(exc, FileNotFoundError):
            executable = frame.f_locals.get("executable")
            if isinstance(executable, str) and Path(executable).name.casefold() in {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}:
                result["missing_component"] = Path(executable).stem.casefold()
        traceback = traceback.tb_next
    if frames:
        result["frames"] = frames[-8:]
    return result


def main() -> None:
    # The old `python -m` entrypoint put cwd on sys.path; preserve that behavior.
    sys.path.insert(0, str(Path.cwd()))
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--olivia-path-safe-io", action="store_true")
        args, rest = parser.parse_known_args()
        sys.argv[1:] = rest
        if args.olivia_path_safe_io:
            paths = argparse.ArgumentParser(add_help=False)
            paths.add_argument("--temp_dir", required=True)
            paths.add_argument("--video_out_path", required=True)
            output, _ = paths.parse_known_args()
            from latentsync.pipelines import lipsync_pipeline
            install_path_safe_video_io(lipsync_pipeline, temp_dir=output.temp_dir, output_path=output.video_out_path)
        runpy.run_module("scripts.inference", run_name="__main__", alter_sys=True)
    except Exception as exc:
        print(MARKER + json.dumps(project_exception(exc), sort_keys=True), file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    main()
