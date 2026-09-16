from __future__ import annotations

from pathlib import Path
from typing import Mapping


def resolve_media_path(value: object, environ: Mapping[str, str], *, follow_symlinks: bool = True) -> Path | None:
    """Resolve one configured path without consulting the process cwd."""

    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        project_root = Path(str(environ.get("OLIVIA_PROJECT_ROOT", ""))).expanduser()
        if not project_root.is_absolute():
            return None
        path = project_root / path
    return path.resolve(strict=False) if follow_symlinks else path.absolute()


def configured_media_path(
    environ: Mapping[str, str],
    name: str,
) -> Path | None:
    """Resolve a configured media path without depending on process cwd."""

    # Executables must retain their venv location on Linux.
    return resolve_media_path(environ.get(name, ""), environ,
                              follow_symlinks=not name.endswith(('_PYTHON', '_EXE')))
