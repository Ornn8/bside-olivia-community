"""Run upstream inference unchanged, with a bounded private-data-free failure marker."""
from __future__ import annotations

import json
from pathlib import Path
import re
import runpy
import sys

MARKER = "OLIVIA_LATENTSYNC_FAILURE="


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
        runpy.run_module("scripts.inference", run_name="__main__", alter_sys=True)
    except Exception as exc:
        print(MARKER + json.dumps(project_exception(exc), sort_keys=True), file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    main()
