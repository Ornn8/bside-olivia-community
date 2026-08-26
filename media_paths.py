from __future__ import annotations

from pathlib import Path
from typing import Mapping


def configured_media_path(
    environ: Mapping[str, str],
    name: str,
) -> Path | None:
    """Resolve a configured media path without depending on process cwd."""

    raw = str(environ.get(name, "")).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    project_root = Path(str(environ.get("OLIVIA_PROJECT_ROOT", ""))).expanduser()
    if not project_root.is_absolute():
        return None
    return project_root / path
