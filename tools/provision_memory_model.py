"""Provision or verify the pinned local FastEmbed model used by Mem0.

This installer-only command prints stable JSON status codes and never prints an
absolute path, model content, credential, or user data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_model import (  # noqa: E402
    MemoryModelError,
    extract_model_archive,
    load_memory_model_manifest,
    validate_model_cache,
    verify_fastembed_model,
    write_model_marker,
)


_DOWNLOAD_TIMEOUT_SECONDS = 180
_USER_AGENT = "BSideOliviaLocal-MemoryModel/1"


def _emit(status: str, *, error_code: str | None = None) -> None:
    payload: dict[str, str] = {"status": status}
    if error_code is not None:
        payload["error_code"] = error_code
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _download(manifest, destination: Path) -> None:
    request = Request(
        manifest.archive_url,
        headers={"User-Agent": _USER_AGENT},
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            final = urlsplit(response.geturl())
            expected = urlsplit(manifest.archive_url)
            if (
                final.scheme != "https"
                or final.hostname != expected.hostname
                or final.path != expected.path
                or final.query
                or final.fragment
            ):
                raise MemoryModelError("MEMORY_MODEL_DOWNLOAD_REDIRECT_FORBIDDEN")
            header = response.headers.get("Content-Length")
            if header is not None:
                try:
                    if int(header) != manifest.archive_size:
                        raise MemoryModelError(
                            "MEMORY_MODEL_ARCHIVE_SIZE_MISMATCH"
                        )
                except ValueError as exc:
                    raise MemoryModelError(
                        "MEMORY_MODEL_DOWNLOAD_INVALID"
                    ) from exc
            with destination.open("xb") as stream:
                while True:
                    block = response.read(1 << 20)
                    if not block:
                        break
                    total += len(block)
                    if total > manifest.archive_size:
                        raise MemoryModelError(
                            "MEMORY_MODEL_ARCHIVE_SIZE_MISMATCH"
                        )
                    digest.update(block)
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
    except MemoryModelError:
        raise
    except Exception as exc:
        raise MemoryModelError("MEMORY_MODEL_DOWNLOAD_FAILED") from exc
    if total != manifest.archive_size:
        raise MemoryModelError("MEMORY_MODEL_ARCHIVE_SIZE_MISMATCH")
    if digest.hexdigest() != manifest.archive_sha256:
        raise MemoryModelError("MEMORY_MODEL_ARCHIVE_HASH_MISMATCH")


def provision(manifest_path: Path, cache_root: Path) -> None:
    manifest = load_memory_model_manifest(manifest_path)
    cache_root = cache_root.expanduser().resolve()
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(
            tempfile.mkdtemp(
                prefix=".olivia-memory-download-",
                dir=cache_root.parent,
            )
        )
    except OSError as exc:
        raise MemoryModelError("MEMORY_MODEL_CACHE_UNWRITABLE") from exc
    archive = temporary_root / "model.tar.gz"
    try:
        _download(manifest, archive)
        extract_model_archive(archive, cache_root, manifest)
        verify_fastembed_model(cache_root, manifest)
        write_model_marker(cache_root, manifest)
    except Exception:
        marker = cache_root / ".olivia-memory-model.json"
        marker.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def verify(manifest_path: Path, cache_root: Path) -> None:
    manifest = load_memory_model_manifest(manifest_path)
    status = validate_model_cache(cache_root, manifest)
    if not status.ready:
        raise MemoryModelError(
            status.reason_code or "MEMORY_MODEL_NOT_READY"
        )
    verify_fastembed_model(cache_root.expanduser().resolve(), manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--provision", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.provision:
            provision(args.manifest, args.cache_root)
        else:
            verify(args.manifest, args.cache_root)
        _emit("READY")
        return 0
    except MemoryModelError as exc:
        _emit("UNAVAILABLE", error_code=exc.code)
        return 2
    except (OSError, RuntimeError, TypeError, ValueError):
        _emit("UNAVAILABLE", error_code="MEMORY_MODEL_PROVISIONING_FAILED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
