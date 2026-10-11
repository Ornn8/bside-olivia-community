"""Publish verified native entrypoints from an installed backend payload."""
from __future__ import annotations

import hashlib
from pathlib import Path
import uuid


def install_native_launcher(root: Path, backend: Path) -> None:
    source = backend / "installer/olivia_launcher/Olivia.exe"
    content = source.read_bytes()
    expected = source.with_suffix(".exe.sha256").read_text(encoding="ascii").strip()
    if not content.startswith(b"MZ") or hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("NATIVE_LAUNCHER_HASH_MISMATCH")
    for target, data in (
        (root / "Olivia.exe", content),
        (root / "olivia.ico", (backend / "installer/assets/olivia.ico").read_bytes()),
        (root / "launcher/Create-Shortcut.ps1", (backend / "installer/Create-Shortcut.ps1").read_bytes()),
    ):
        if target.is_file() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex)
        try:
            temporary.write_bytes(data)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def native_launcher_current(root: Path, backend: Path) -> bool:
    """True once this backend's entrypoint is published; startup then skips the shortcut script."""
    try:
        return (root / "Olivia.exe").read_bytes() == (backend / "installer/olivia_launcher/Olivia.exe").read_bytes()
    except OSError:
        return False
