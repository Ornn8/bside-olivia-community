from __future__ import annotations

from pathlib import Path
from typing import Mapping


def resolve_media_path(value: object, environ: Mapping[str, str]) -> Path | None:
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
    return path.resolve(strict=False)


def configured_media_path(
    environ: Mapping[str, str],
    name: str,
) -> Path | None:
    """Resolve a configured media path without depending on process cwd."""

    return resolve_media_path(environ.get(name, ""), environ)
