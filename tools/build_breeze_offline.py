"""Build a hash-checked wheel supplement, optionally a complete video ZIP.

No downloads: populate --wheelhouse with the locked Windows CPython 3.12 wheels
first. Existing model ZIPs are read only. Output files must not already exist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(wheelhouse: Path, output: Path, full_source: Path | None = None) -> Path:
    manifest_path = Path(__file__).resolve().parents[1] / "installer/video-capability-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    specs = {f["path"]: f for b in manifest["bundles"] for f in b["files"]}
    wheels = {name: spec for name, spec in specs.items() if name.startswith("breeze/wheels/")}
    for name, spec in wheels.items():
        path = wheelhouse / Path(name).name
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != spec["size_bytes"] or digest != spec["sha256"]:
            raise ValueError(f"Wheel does not match manifest: {path.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        written = set()
        if full_source is not None:
            with zipfile.ZipFile(full_source) as source:
                for entry in source.infolist():
                    name = entry.filename
                    if name in wheels or name == "olivia-video-offline-manifest.json":
                        continue
                    if name in written:
                        raise ValueError(f"Duplicate source member: {name}")
                    digest = hashlib.sha256()
                    size = 0
                    with source.open(entry) as src, archive.open(name, "w", force_zip64=True) as dst:
                        while chunk := src.read(1024 * 1024):
                            dst.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    if name in specs and (size != specs[name]["size_bytes"] or digest.hexdigest() != specs[name]["sha256"]):
                        raise ValueError(f"Source member does not match manifest: {name}")
                    written.add(name)
        for name in wheels:
            archive.write(wheelhouse / Path(name).name, name)
            written.add(name)
        required = set(specs) if full_source is not None else set(wheels)
        if required - written:
            raise ValueError(f"Missing files: {sorted(required - written)}")
        archive.writestr("olivia-video-offline-manifest.json", json.dumps({
            "schema_version": "olivia.video-offline-private.v1",
            "version": manifest["version"],
            "video_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "files": [specs[name] for name in sorted(required)],
        }, ensure_ascii=False, indent=2))
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full-source", type=Path)
    args = parser.parse_args()
    print(build(args.wheelhouse, args.output, args.full_source))
