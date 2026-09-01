"""Standalone uninstall entry point for the managed-runtime Windows install."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from .uninstall_safety import (
        MARKER_NAME,
        OWNED_PATHS,
        PRESERVED_PATHS,
        remove_owned_targets,
        safe_owned_targets,
    )
except ImportError:  # Support direct execution and the stable runpy launcher.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from uninstall_safety import (
        MARKER_NAME,
        OWNED_PATHS,
        PRESERVED_PATHS,
        remove_owned_targets,
        safe_owned_targets,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.installation.expanduser().absolute()
    marker_path = root / MARKER_NAME
    if not root.is_dir() or not marker_path.is_file():
        print("PATCH_MARKER_NOT_FOUND")
        return 2
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("schema_version") != "olivia.full-patch.install.v2" or marker.get("owned_root") != str(root):
        print("PATCH_MARKER_INVALID")
        return 2
    try:
        safe_owned_targets(root)
    except ValueError:
        print("PATCH_MARKER_INVALID")
        return 2
    print(json.dumps({"status": "UNINSTALLED" if args.apply else "DRY_RUN", "owned_paths": list(OWNED_PATHS), "preserved_paths": list(PRESERVED_PATHS)}, ensure_ascii=False))
    if args.apply:
        try:
            remove_owned_targets(root)
        except ValueError:
            print("PATCH_MARKER_INVALID")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
