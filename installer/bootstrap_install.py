from __future__ import annotations

import runpy
import errno
import json
import sys
from pathlib import Path


payload_root = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(payload_root))
try:
    runpy.run_module("installer", run_name="__main__")
except Exception as exc:
    # Do not print exception messages: they can contain private paths or data.
    number = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    if winerror in {32, 33}:
        code = "SETUP_PATCH_FILE_IN_USE"
    elif isinstance(exc, PermissionError):
        code = "SETUP_PATCH_PERMISSION_DENIED"
    elif number == errno.ENOSPC or winerror in {39, 112}:
        code = "SETUP_PATCH_DISK_FULL"
    elif isinstance(exc, FileNotFoundError):
        code = "SETUP_PATCH_FILE_MISSING"
    else:
        code = "SETUP_PATCH_UNEXPECTED_ERROR"
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        path = Path(trace.tb_frame.f_code.co_filename).resolve()
        if path.is_relative_to(payload_root):
            frames.append({"file": path.relative_to(payload_root).as_posix(), "line": trace.tb_lineno})
        trace = trace.tb_next
    diagnostic = {"schema_version": "olivia.setup-patch-error.v1", "phase": "INSTALL_PATCH",
                  "error_type": type(exc).__name__, "errno": number, "winerror": winerror,
                  "frames": frames[-6:]}
    print(json.dumps({"status": "ERROR", "code": code, "diagnostic": diagnostic}, ensure_ascii=True))
    raise SystemExit(2)
