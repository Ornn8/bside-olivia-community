from __future__ import annotations

import runpy
import errno
import json
import re
import shutil
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
    copy_errors = []
    if isinstance(exc, shutil.Error) and exc.args and isinstance(exc.args[0], list):
        for entry in exc.args[0][:20]:
            if not isinstance(entry, (list, tuple)) or len(entry) != 3:
                continue
            # copytree flattens OSError objects into strings. Recover numeric
            # codes only; never emit paths or the original exception message.
            message = str(entry[2])
            win_match = re.search(r"\[WinError (\d+)\]", message)
            errno_match = re.search(r"\[Errno (\d+)\]", message)
            copy_errors.append({"winerror": int(win_match[1]) if win_match else None,
                                "errno": int(errno_match[1]) if errno_match else None})
        if copy_errors:
            winerror = copy_errors[0]["winerror"]
            number = copy_errors[0]["errno"]
    if winerror in {32, 33}:
        code = "SETUP_PATCH_FILE_IN_USE"
    elif isinstance(exc, PermissionError) or number in {errno.EACCES, errno.EPERM} or winerror == 5:
        code = "SETUP_PATCH_PERMISSION_DENIED"
    elif number == errno.ENOSPC or winerror in {39, 112}:
        code = "SETUP_PATCH_DISK_FULL"
    elif isinstance(exc, FileNotFoundError) or number == errno.ENOENT or winerror in {2, 3}:
        code = "SETUP_PATCH_FILE_MISSING"
    elif number == errno.ENAMETOOLONG or winerror == 206:
        code = "SETUP_PATCH_PATH_TOO_LONG"
    elif isinstance(exc, shutil.Error):
        code = "SETUP_PATCH_COPY_FAILED"
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
    if isinstance(exc, shutil.Error):
        diagnostic["operation"] = "copy_tree"
        diagnostic["copy_errors"] = copy_errors
    print(json.dumps({"status": "ERROR", "code": code, "diagnostic": diagnostic}, ensure_ascii=True))
    raise SystemExit(2)
