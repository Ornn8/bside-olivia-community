"""Standalone uninstall entry point for the managed-runtime Windows install."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
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


def _remove_launch_shortcuts(root: Path) -> None:
    if os.name != "nt":
        return
    script = Path(__file__).resolve().parent / "Create-Shortcut.ps1"
    try:
        powershell = (
            Path(os.environ["WINDIR"])
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not powershell.is_file() or not script.is_file():
            raise OSError
        subprocess.run(
            [
                os.fspath(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                os.fspath(script),
                "-InstallRoot",
                os.fspath(root),
                "-RemoveExisting",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (KeyError, OSError, subprocess.SubprocessError):
        print("SHORTCUT_CLEANUP_FAILED")


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
        _remove_launch_shortcuts(root)
        try:
            remove_owned_targets(root)
        except ValueError:
            print("PATCH_MARKER_INVALID")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
