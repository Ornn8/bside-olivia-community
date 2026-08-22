"""Standalone uninstall entry point for the managed-runtime Windows install."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.installation.expanduser().resolve()
    marker_path = root / ".olivia-full-patch.json"
    if not root.is_dir() or not marker_path.is_file():
        print("PATCH_MARKER_NOT_FOUND")
        return 2
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("schema_version") != "olivia.full-patch.install.v2" or marker.get("owned_root") != str(root):
        print("PATCH_MARKER_INVALID")
        return 2
    owned = tuple(marker.get("owned_paths", ("app", "local_backend", "START.cmd", "UNINSTALL.cmd", marker_path.name)))
    print(json.dumps({"status": "UNINSTALLED" if args.apply else "DRY_RUN", "owned_paths": list(owned), "preserved_paths": ["data", "logs", "third-party"]}, ensure_ascii=False))
    if args.apply:
        for name in owned:
            target = root / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.is_file():
                target.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
