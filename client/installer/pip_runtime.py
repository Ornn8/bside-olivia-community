"""Install runtime wheels and compile them using normalized Windows paths.

pip's RECORD paths contain forward slashes. Joined to an extended-length
Windows TEMP path, those names fail its isfile check and silently skip pyc.
Compile the final target with native separators after a successful install.
"""
from __future__ import annotations

import compileall
import os
import runpy
import sys


def main() -> int:
    arguments = sys.argv[1:]
    target = arguments[arguments.index("--target") + 1]
    sys.argv = ["pip", *arguments]
    try:
        runpy.run_module("pip", run_name="__main__")
    except SystemExit as exc:
        if exc.code:
            return exc.code if isinstance(exc.code, int) else 1
    return 0 if compileall.compile_dir(os.path.normpath(target), quiet=2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
