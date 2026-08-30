"""Verify the install-owned Mem0 runtime without native ``python -c`` quoting."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def verify_runtime(runtime: Path, requirements: Path) -> bool:
    runtime = runtime.resolve()
    requirements = requirements.resolve()
    try:
        manifest = json.loads(
            (runtime / ".olivia-mem0-runtime-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        expected_hash = hashlib.sha256(requirements.read_bytes()).hexdigest()
        capability = json.loads(
            (requirements.parent / "mem0-capability-manifest.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict) or set(manifest) != {
        "bytecode_policy",
        "requirements_sha256",
        "source",
    }:
        return False
    source = manifest.get("source")
    runtime_bom = capability.get("runtime") if isinstance(capability, dict) else None
    sources = runtime_bom.get("pypi_sources") if isinstance(runtime_bom, dict) else None
    if (
        not isinstance(capability, dict)
        or capability.get("schema_version") != "olivia.capability-bom.v1"
        or not isinstance(runtime_bom, dict)
        or runtime_bom.get("requirements_sha256") != expected_hash
        or runtime_bom.get("requirements")
        != "installer/mem0-runtime-requirements.txt"
        or not isinstance(sources, dict)
        or set(sources) != {"mirror", "official"}
        or any(not isinstance(item, str) for item in sources.values())
        or manifest.get("bytecode_policy") != "pip-compile-v1"
        or not isinstance(source, str)
        or source not in sources.values()
    ):
        return False
    parsed_source = urlsplit(source)
    if (
        manifest.get("requirements_sha256") != expected_hash
        or parsed_source.scheme != "https"
        or not parsed_source.hostname
        or parsed_source.username
        or parsed_source.password
        or parsed_source.query
        or parsed_source.fragment
    ):
        return False

    sys.path[:0] = [
        str(runtime),
        str(runtime / "win32"),
        str(runtime / "win32" / "lib"),
    ]
    for line in requirements.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s]+)", line)
        if match is None:
            continue
        try:
            if importlib.metadata.version(match.group(1)) != match.group(2):
                return False
        except importlib.metadata.PackageNotFoundError:
            return False
    for module in ("mem0", "sentence_transformers", "huggingface_hub", "pywintypes"):
        spec = importlib.util.find_spec(module)
        if spec is None or not spec.origin:
            return False
        if runtime not in Path(spec.origin).resolve().parents:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        return 2
    return 0 if verify_runtime(Path(args[0]), Path(args[1])) else 2


if __name__ == "__main__":
    raise SystemExit(main())
