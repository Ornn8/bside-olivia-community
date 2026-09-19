"""Build the network-free core asset directory consumed by Install.ps1."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


SCHEMA_VERSION = "olivia.offline-core-assets.v1"
MANIFEST_NAME = "offline-core-assets.json"
PYTHON_RUNTIME = {
    "filename": "python-3.12.10-embed-amd64.zip",
    "source_url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
    "sha256": "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
}
PIP_BOOTSTRAP = {
    "filename": "pip-25.2-py3-none-any.whl",
    "package": "pip",
    "version": "25.2",
    "sha256": "6d67a2b4e7f14d8b31b8b52648866fa717f45a1eb70e83002f4331d07e953717",
}
_HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")


class OfflineCoreBuildError(RuntimeError):
    """Stable build-time failure code."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "bside-olivia-offline-core-builder/1"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def _download_checked(spec: dict[str, str], destination: Path) -> dict[str, object]:
    _download(spec["source_url"], destination)
    digest = _sha256(destination)
    if digest != spec["sha256"]:
        raise OfflineCoreBuildError("OFFLINE_CORE_SOURCE_HASH_MISMATCH")
    return {
        "path": destination.name,
        "size_bytes": destination.stat().st_size,
        "sha256": digest,
        "source_url": spec["source_url"],
    }


def _download_pip_bootstrap(staging: Path) -> dict[str, object]:
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--no-deps",
        "--dest",
        os.fspath(staging),
        f"{PIP_BOOTSTRAP['package']}=={PIP_BOOTSTRAP['version']}",
    ]
    result = subprocess.run(command, check=False, timeout=120)
    destination = staging / PIP_BOOTSTRAP["filename"]
    if result.returncode != 0 or not destination.is_file():
        raise OfflineCoreBuildError("OFFLINE_CORE_PIP_DOWNLOAD_FAILED")
    digest = _sha256(destination)
    if digest != PIP_BOOTSTRAP["sha256"]:
        raise OfflineCoreBuildError("OFFLINE_CORE_SOURCE_HASH_MISMATCH")
    return {
        "path": destination.name,
        "size_bytes": destination.stat().st_size,
        "sha256": digest,
        "package": PIP_BOOTSTRAP["package"],
        "version": PIP_BOOTSTRAP["version"],
    }


def _locked_hashes(requirements: Path) -> set[str]:
    values = {
        match.group(1)
        for line in requirements.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
        for match in [_HASH_RE.search(line)]
        if match is not None
    }
    if not values:
        raise OfflineCoreBuildError("OFFLINE_CORE_REQUIREMENTS_INVALID")
    return values


def build_offline_core_assets(output: Path, requirements: Path) -> dict[str, object]:
    output = output.expanduser().resolve()
    requirements = requirements.expanduser().resolve()
    if output.exists():
        raise OfflineCoreBuildError("OFFLINE_CORE_OUTPUT_EXISTS")
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    wheelhouse = staging / "wheelhouse"
    try:
        wheelhouse.mkdir(parents=True)
        runtime = _download_checked(PYTHON_RUNTIME, staging / PYTHON_RUNTIME["filename"])
        pip_bootstrap = _download_pip_bootstrap(staging)
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "--platform",
            "win_amd64",
            "--python-version",
            "3.12",
            "--implementation",
            "cp",
            "--abi",
            "cp312",
            "--dest",
            os.fspath(wheelhouse),
            "-r",
            os.fspath(requirements),
        ]
        result = subprocess.run(command, check=False, timeout=300)
        if result.returncode != 0:
            raise OfflineCoreBuildError("OFFLINE_CORE_WHEEL_DOWNLOAD_FAILED")
        wheels = sorted(wheelhouse.glob("*.whl"))
        expected_hashes = _locked_hashes(requirements)
        actual_hashes = {_sha256(path) for path in wheels}
        if actual_hashes != expected_hashes or len(wheels) != len(expected_hashes):
            raise OfflineCoreBuildError("OFFLINE_CORE_WHEEL_SET_MISMATCH")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "python_runtime": runtime,
            "pip_bootstrap": pip_bootstrap,
            "requirements_sha256": _sha256(requirements),
            "wheels": [
                {
                    "path": f"wheelhouse/{path.name}",
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for path in wheels
            ],
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output)
        return manifest
    except OfflineCoreBuildError:
        raise
    except (OSError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        raise OfflineCoreBuildError("OFFLINE_CORE_BUILD_FAILED") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build-offline-core-assets")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--requirements",
        type=Path,
        default=Path(__file__).with_name("runtime-requirements.txt"),
    )
    args = parser.parse_args(argv)
    try:
        result = build_offline_core_assets(args.output, args.requirements)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except OfflineCoreBuildError as exc:
        print(json.dumps({"status": "ERROR", "code": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
