from __future__ import annotations

import runpy
import sys
from pathlib import Path


payload_root = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(payload_root))
runpy.run_module("installer", run_name="__main__")
