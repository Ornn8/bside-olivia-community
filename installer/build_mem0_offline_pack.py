"""Build a verified Mem0 offline capability pack for Windows releases."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import uuid
import zipfile

from mem0_capability_install import Mem0CapabilityBOM, load_mem0_capability_bom


_HASH_RE = re.compile(rb"--hash=sha256:([0-9a-f]{64})(?:\s|$)")


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_mem0_offline_pack(
    *,
    output: Path,
    wheelhouse: Path,
    model_root: Path,
    bom: Mem0CapabilityBOM,
    requirements_bytes: bytes,
) -> Path:
    """Create one atomic pack only from the exact locked wheel/model closure."""

    if (
        output.suffix.casefold() != ".oliviapack"
        or hashlib.sha256(requirements_bytes).hexdigest()
        != bom.runtime.requirements_sha256
    ):
        raise RuntimeError("MEM0_OFFLINE_PACKAGE_INVALID")
    wheels = sorted(path for path in wheelhouse.iterdir() if path.is_file())
    allowed_hashes = {match.decode() for match in _HASH_RE.findall(requirements_bytes)}
    if (
        len(wheels) != bom.runtime.package_count
        or any(path.suffix.casefold() != ".whl" for path in wheels)
        or {_sha256(path) for path in wheels} != allowed_hashes
    ):
        raise RuntimeError("MEM0_OFFLINE_WHEELHOUSE_INVALID")
    model_files = sorted(path for path in model_root.rglob("*") if path.is_file())
    observed_model = {
        path.relative_to(model_root).as_posix(): (_sha256(path), path.stat().st_size)
        for path in model_files
    }
    expected_model = {
        name: (artifact.sha256, artifact.size_bytes)
        for name, artifact in bom.model.files.items()
    }
    if observed_model != expected_model:
        raise RuntimeError("MEM0_OFFLINE_MODEL_INVALID")
    metadata = {
        "schema_version": "olivia.offline-capability-pack.v1",
        "capability": "long_term_memory",
        "version": bom.version,
        "requirements_sha256": bom.runtime.requirements_sha256,
        "model_revision": bom.model.revision,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            )
            archive.writestr("requirements.txt", requirements_bytes)
            for path in wheels:
                archive.write(path, f"wheelhouse/{path.name}")
            for path in model_files:
                archive.write(path, f"model/{path.relative_to(model_root).as_posix()}")
        staging.replace(output)
    finally:
        staging.unlink(missing_ok=True)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    requirements = root / "mem0-runtime-requirements.txt"
    bom = load_mem0_capability_bom(root / "mem0-capability-manifest.json", requirements)
    build_mem0_offline_pack(
        output=args.output,
        wheelhouse=args.wheelhouse,
        model_root=args.model_root,
        bom=bom,
        requirements_bytes=requirements.read_bytes(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
