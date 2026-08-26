"""Serve completed reply media to the original Olivia player over loopback HTTP.

The original webplayer can receive a URL-like ``uid`` after its local fallback
is installed. This module provides the narrow URL and byte-range contract
needed by a native video element. It does not discover files, mutate letter
state, or decide whether a reply should be video.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import mimetypes
import os
from pathlib import Path
import re
from typing import Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

from aiohttp import web


MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MEDIA_PATH = "/toy/media/{media_id}"
DEFAULT_CHUNK_SIZE = 512 * 1024
_ALLOWED_SUFFIXES = frozenset({".mp4", ".webm", ".m4v", ".mov"})
_ALLOWED_CONTENT_TYPES = frozenset(
    {"video/mp4", "video/webm", "video/quicktime", "video/x-m4v"}
)


class OriginalClientMediaError(RuntimeError):
    """Stable media-serving failure without a local filesystem path."""

    def __init__(self, code: str, *, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class ResolvedReplyMedia:
    media_id: str
    path: Path
    content_type: str | None = None


@runtime_checkable
class ReplyMediaResolver(Protocol):
    def resolve_reply_media(self, media_id: str) -> ResolvedReplyMedia | None: ...


@dataclass(frozen=True)
class FileRange:
    start: int
    end: int
    size: int
    requested: bool

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True)
class ValidatedMediaFile:
    media_id: str
    path: Path
    size: int
    modified_ns: int
    content_type: str
    etag: str


def _media_id(value: object) -> str:
    if not isinstance(value, str) or not MEDIA_ID_RE.fullmatch(value):
        raise OriginalClientMediaError("MEDIA_ID_INVALID", status=404)
    return value


def original_webplayer_uid(base_url: str, media_id: str) -> str:
    """Build the only URL form accepted by the patched original webplayer."""

    identifier = _media_id(media_id)
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise OriginalClientMediaError("MEDIA_BASE_URL_INVALID", status=500) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise OriginalClientMediaError("MEDIA_BASE_URL_INVALID", status=500)
    return (
        f"http://{parsed.hostname}:{port}/toy/media/"
        f"{quote(identifier, safe='._:-')}"
    )


def _content_type(path: Path, declared: str | None) -> str:
    if declared:
        normalized = declared.strip().casefold()
        if normalized in _ALLOWED_CONTENT_TYPES:
            return normalized
        raise OriginalClientMediaError("MEDIA_CONTENT_TYPE_INVALID", status=500)
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed in _ALLOWED_CONTENT_TYPES:
        return guessed
    raise OriginalClientMediaError("MEDIA_CONTENT_TYPE_INVALID", status=500)


def _validated_file(
    resolver: ReplyMediaResolver,
    media_root: Path,
    media_id: str,
) -> ValidatedMediaFile:
    try:
        resolved = resolver.resolve_reply_media(media_id)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise OriginalClientMediaError("MEDIA_RESOLVER_UNAVAILABLE", status=503) from exc
    if resolved is None:
        raise OriginalClientMediaError("MEDIA_NOT_FOUND", status=404)
    if not isinstance(resolved, ResolvedReplyMedia) or resolved.media_id != media_id:
        raise OriginalClientMediaError("MEDIA_RESOLVER_INVALID", status=503)

    root = media_root.expanduser().resolve()
    try:
        path = resolved.path.expanduser().resolve(strict=True)
        common = os.path.commonpath([str(root), str(path)])
    except (OSError, RuntimeError, ValueError) as exc:
        raise OriginalClientMediaError("MEDIA_NOT_FOUND", status=404) from exc
    if (
        common != str(root)
        or not path.is_file()
        or path.suffix.casefold() not in _ALLOWED_SUFFIXES
    ):
        raise OriginalClientMediaError("MEDIA_NOT_FOUND", status=404)
    try:
        stat = path.stat()
    except OSError as exc:
        raise OriginalClientMediaError("MEDIA_NOT_FOUND", status=404) from exc
    if stat.st_size <= 0:
        raise OriginalClientMediaError("MEDIA_NOT_FOUND", status=404)

    content_type = _content_type(path, resolved.content_type)
    digest = hashlib.sha256(
        f"{media_id}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:32]
    return ValidatedMediaFile(
        media_id=media_id,
        path=path,
        size=int(stat.st_size),
        modified_ns=int(stat.st_mtime_ns),
        content_type=content_type,
        etag=f'"{digest}"',
    )


def _parse_range(value: str | None, size: int) -> FileRange:
    if not value:
        return FileRange(0, size - 1, size, False)
    if not value.startswith("bytes=") or "," in value:
        raise OriginalClientMediaError("MEDIA_RANGE_INVALID", status=416)
    spec = value[6:].strip()
    if spec.count("-") != 1:
        raise OriginalClientMediaError("MEDIA_RANGE_INVALID", status=416)
    left, right = spec.split("-", 1)
    try:
        if left:
            start = int(left)
            end = int(right) if right else size - 1
        elif right:
            suffix = int(right)
            if suffix <= 0:
                raise ValueError
            suffix = min(suffix, size)
            start = size - suffix
            end = size - 1
        else:
            raise ValueError
    except ValueError as exc:
        raise OriginalClientMediaError("MEDIA_RANGE_INVALID", status=416) from exc
    if start < 0 or end < start or start >= size:
        raise OriginalClientMediaError("MEDIA_RANGE_INVALID", status=416)
    return FileRange(start, min(end, size - 1), size, True)


def _headers(media: ValidatedMediaFile, file_range: FileRange) -> dict[str, str]:
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Length": str(file_range.length),
        "Content-Type": media.content_type,
        "ETag": media.etag,
        "X-Content-Type-Options": "nosniff",
    }
    if file_range.requested:
        headers["Content-Range"] = (
            f"bytes {file_range.start}-{file_range.end}/{file_range.size}"
        )
    return headers


def _error_response(
    exc: OriginalClientMediaError,
    *,
    size: int | None = None,
    head: bool = False,
) -> web.Response:
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if exc.status == 416:
        headers["Content-Range"] = f"bytes */{size if size is not None else '*'}"
    if head:
        return web.Response(status=exc.status, headers=headers)
    return web.json_response(
        {"status": "FAILED", "error_code": exc.code},
        status=exc.status,
        headers=headers,
    )


def create_reply_media_handler(
    *,
    resolver: ReplyMediaResolver,
    media_root: str | os.PathLike[str],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
):
    """Create an aiohttp GET/HEAD handler without opening a second service."""

    if not isinstance(resolver, ReplyMediaResolver):
        raise TypeError("a ReplyMediaResolver is required")
    root = Path(media_root).expanduser().resolve()
    if chunk_size < 64 * 1024 or chunk_size > 4 * 1024 * 1024:
        raise ValueError("media chunk size is out of bounds")

    async def handle(request: web.Request) -> web.StreamResponse:
        media: ValidatedMediaFile | None = None
        try:
            identifier = _media_id(request.match_info.get("media_id"))
            media = _validated_file(resolver, root, identifier)
            if_none_match = request.headers.get("If-None-Match")
            if (
                if_none_match
                and if_none_match.strip() == media.etag
                and not request.headers.get("Range")
            ):
                return web.Response(
                    status=304,
                    headers={
                        "Cache-Control": "private, no-store",
                        "ETag": media.etag,
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            file_range = _parse_range(request.headers.get("Range"), media.size)
        except OriginalClientMediaError as exc:
            return _error_response(
                exc,
                size=media.size if media is not None else None,
                head=request.method == "HEAD",
            )

        status = 206 if file_range.requested else 200
        headers = _headers(media, file_range)
        if request.method == "HEAD":
            return web.Response(status=status, headers=headers)

        try:
            stream = media.path.open("rb")
            stream.seek(file_range.start)
        except OSError:
            return _error_response(
                OriginalClientMediaError("MEDIA_READ_FAILED", status=500)
            )

        response = web.StreamResponse(status=status, headers=headers)
        try:
            await response.prepare(request)
            remaining = file_range.length
            while remaining:
                block = stream.read(min(chunk_size, remaining))
                if not block:
                    response.force_close()
                    return response
                await response.write(block)
                remaining -= len(block)
            await response.write_eof()
            return response
        except (ConnectionError, OSError, RuntimeError):
            response.force_close()
            return response
        finally:
            stream.close()

    return handle


def mount_reply_media_route(
    app: web.Application,
    *,
    resolver: ReplyMediaResolver,
    media_root: str | os.PathLike[str],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> None:
    """Mount the original-player media route once on the existing local app."""

    if "original_reply_media_mounted" in app:
        raise RuntimeError("MEDIA_ROUTE_ALREADY_MOUNTED")
    handler = create_reply_media_handler(
        resolver=resolver,
        media_root=media_root,
        chunk_size=chunk_size,
    )
    app["original_reply_media_mounted"] = True
    app.router.add_get(MEDIA_PATH, handler, allow_head=True)


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "MEDIA_PATH",
    "OriginalClientMediaError",
    "ReplyMediaResolver",
    "ResolvedReplyMedia",
    "create_reply_media_handler",
    "mount_reply_media_route",
    "original_webplayer_uid",
]
