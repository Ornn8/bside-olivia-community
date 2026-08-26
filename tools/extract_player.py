"""Extract player archives into a caller-controlled, contained output root."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


def _safe_target(root: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(normalized)
    raw_parts = normalized.split("/")
    if (
        not normalized
        or "\x00" in normalized
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {".", ".."} for part in raw_parts)
    ):
        raise ValueError(f"unsafe archive member: {member_name!r}")
    target = (root / Path(*posix.parts)).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise ValueError(f"archive member escapes output root: {member_name!r}")
    return target


def safe_extract_zip(
    archive_path: str | os.PathLike[str],
    output_root: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Extract a zip only under ``output_root`` and an optional allowed root."""
    archive = Path(archive_path).resolve()
    root = Path(output_root).resolve()
    allowed = Path(allowed_root or Path.cwd()).resolve()
    if os.path.commonpath([str(allowed), str(root)]) != str(allowed):
        raise ValueError("output root must stay under the allowed root")
    if not archive.is_file():
        raise FileNotFoundError(archive)

    extracted: list[Path] = []
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        for info in members:
            target = _safe_target(root, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            extracted.append(target)
    return extracted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("0.0.9.615/resources"))
    parser.add_argument("--output-root", type=Path, default=Path("extracted_player"))
    args = parser.parse_args()

    base = args.base.resolve()
    output_root = args.output_root.resolve()
    allowed_root = Path.cwd().resolve()
    results = {}
    for filename in ("feplayer.dat", "webplayer.dat"):
        archive = base / filename
        target = output_root / filename.removesuffix(".dat")
        files = safe_extract_zip(archive, target, allowed_root=allowed_root)
        results[filename] = {"output": str(target), "files": len(files)}
    print(json.dumps({"status": "EXTRACTED", "archives": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
