"""Verify every packed byte, unpack into a fresh folder, and emit local evidence."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile


def verify(archive, destination):
    if destination.exists():
        raise ValueError("Use a fresh directory")
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read("package-manifest.json"))
        names = z.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest["files"]) | {"package-manifest.json"}:
            raise ValueError("Package membership mismatch")
        for name, item in manifest["files"].items():
            parts = PurePosixPath(name).parts
            if PurePosixPath(name).is_absolute() or ".." in parts or "\\" in name or ":" in name:
                raise ValueError("Unsafe package path")
            digest = hashlib.sha256()
            size = 0
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as source, target.open("xb") as output:
                while block := source.read(4 * 1024 * 1024):
                    digest.update(block)
                    size += len(block)
                    output.write(block)
            if digest.hexdigest() != item["sha256"] or size != item["bytes"]:
                raise ValueError("Package hash mismatch: " + name)
        (destination / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    evidence = {"files_verified": len(manifest["files"]), "archive_bytes": archive.stat().st_size,
                "hardware_verified": False}
    archive.with_suffix(".verification.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return evidence


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("archive", type=Path)
    p.add_argument("destination", type=Path)
    a = p.parse_args()
    print(verify(a.archive, a.destination))
