"""Download and verify third-party payloads into an external data root.

The command is deliberately dependency-free and defaults to a no-network dry run.
It never writes the repository; the caller must provide a data root outside it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
import tempfile
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_MANIFEST_KEYS = {"schema_version", "items"}
_ITEM_KEYS = {"id", "source_url", "version", "revision", "license", "target_path", "sha256", "size_bytes"}


class ManifestError(ValueError):
    """Raised for an invalid or unsafe manifest."""


def _validate_source_url(source_url: Any) -> str:
    if not isinstance(source_url, str) or not source_url.strip():
        raise ManifestError("source_url is required")
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ManifestError("source_url must be an HTTPS URL without credentials")
    if parsed.query or parsed.fragment:
        raise ManifestError("source_url must not contain query parameters or fragments")
    if parsed.scheme == "http" and parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ManifestError("source_url must use HTTPS (HTTP is allowed only for loopback fixtures)")
    return source_url.strip()


def _validate_target_path(target_path: Any) -> str:
    if not isinstance(target_path, str) or not target_path.strip():
        raise ManifestError("target_path is required")
    value = target_path.strip().replace("\\", "/")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if value.startswith("/") or value.startswith("\\") or windows.is_absolute() or posix.is_absolute() or windows.drive:
        raise ManifestError("target_path must be relative")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ManifestError("target_path must not escape the data root")
    return "/".join(parts)


def _validate_item(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ManifestError(f"item {index} must be an object")
    if set(item) - _ITEM_KEYS:
        raise ManifestError(f"item {index} has unknown fields")
    item_id = item.get("id")
    if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", item_id):
        raise ManifestError(f"item {index} has an invalid id")
    if not item.get("version") and not item.get("revision"):
        raise ManifestError(f"item {item_id} needs version or revision")
    license_name = item.get("license")
    if not isinstance(license_name, str) or not license_name.strip():
        raise ManifestError(f"item {item_id} needs a license")
    source_url = _validate_source_url(item.get("source_url"))
    target_path = _validate_target_path(item.get("target_path"))
    sha256 = item.get("sha256")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise ManifestError(f"item {item_id} needs a 64-character SHA-256")
    size_bytes = item.get("size_bytes")
    if size_bytes is not None and (isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0):
        raise ManifestError(f"item {item_id} has an invalid size_bytes")
    return {**item, "id": item_id, "source_url": source_url, "target_path": target_path, "sha256": sha256.lower()}


def load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest: {exc.__class__.__name__}") from exc
    if not isinstance(payload, dict) or set(payload) - _MANIFEST_KEYS or payload.get("schema_version") != 1 or not isinstance(payload.get("items"), list):
        raise ManifestError("manifest requires schema_version 1 and an items array")
    items = [_validate_item(item, index) for index, item in enumerate(payload["items"])]
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ManifestError("item ids must be unique")
    return items


def _external_data_root(data_root: Path, repo_root: Path) -> Path:
    root = data_root.expanduser().resolve()
    repo = repo_root.resolve()
    try:
        root.relative_to(repo)
    except ValueError:
        pass
    else:
        raise ManifestError("data_root must be outside the repository")
    try:
        repo.relative_to(root)
    except ValueError:
        return root
    raise ManifestError("data_root must not contain the repository")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        target = urljoin(req.full_url, newurl)
        _validate_source_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _download(item: dict[str, Any], data_root: Path, timeout: float = 30.0) -> Path:
    target = (data_root / item["target_path"]).resolve()
    try:
        target.relative_to(data_root)
    except ValueError as exc:
        raise ManifestError(f"item {item['id']} target escapes data_root") from exc
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise ManifestError(f"item {item['id']} target already exists and is not a regular file")
        _verify_file(target, item)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    opener = build_opener(_SafeRedirectHandler())
    request = Request(item["source_url"], headers={"User-Agent": "bside-olivia-local-downloader/1"})
    temp_name: str | None = None
    try:
        with opener.open(request, timeout=timeout) as response:
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".download-", delete=False) as temp:
                temp_name = temp.name
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if item.get("size_bytes") is not None and total > item["size_bytes"]:
                        raise ManifestError(f"item {item['id']} exceeds declared size")
                    digest.update(chunk)
                    temp.write(chunk)
        if item.get("size_bytes") is not None and total != item["size_bytes"]:
            raise ManifestError(f"item {item['id']} size does not match manifest")
        if digest.hexdigest() != item["sha256"]:
            raise ManifestError(f"item {item['id']} SHA-256 does not match manifest")
        os.replace(temp_name, target)
        temp_name = None
        return target
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ManifestError(f"item {item['id']} download failed: {exc.__class__.__name__}") from exc
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except OSError:
                pass


def _verify_file(path: Path, item: dict[str, Any]) -> None:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                total += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise ManifestError(f"item {item['id']} existing target cannot be read") from exc
    if item.get("size_bytes") is not None and total != item["size_bytes"]:
        raise ManifestError(f"item {item['id']} existing target size does not match manifest")
    if digest.hexdigest() != item["sha256"]:
        raise ManifestError(f"item {item['id']} existing target SHA-256 does not match manifest")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download and verify third-party payloads outside the repository")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="validate and print planned downloads; never contacts sources")
    parser.add_argument("--install", action="store_true", help="download and install after license confirmation")
    parser.add_argument("--accept-licenses", action="store_true", help="confirm that each manifest license was reviewed")
    parser.add_argument("--item", action="append", dest="items", help="limit installation to one or more item ids")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        repo_root = Path(__file__).resolve().parents[1]
        items = load_manifest(args.manifest)
        data_root = _external_data_root(args.data_root, repo_root)
        if args.dry_run and args.install:
            raise ManifestError("--dry-run and --install are mutually exclusive")
        selected = items if not args.items else [item for item in items if item["id"] in set(args.items)]
        if args.items and len(selected) != len(set(args.items)):
            raise ManifestError("unknown item id")
        if args.install and not args.accept_licenses:
            raise ManifestError("installation requires --accept-licenses")
        mode = "install" if args.install else "dry-run"
        print(f"validated {len(selected)} item(s); mode={mode}")
        for item in selected:
            if not args.install:
                print(f"DRY-RUN {item['id']} -> {item['target_path']}")
            else:
                _download(item, data_root)
                print(f"installed {item['id']} -> {item['target_path']}")
        return 0
    except ManifestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
