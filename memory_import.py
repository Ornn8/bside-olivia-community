"""Offline importer for legacy letter files.

Reports contain counts, stable error codes, and source hashes only.  Source
paths, raw bodies, and private identifiers never enter an import report.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from memory_port import LegacyLetter, MemoryPort, MemoryUnavailable


SUPPORTED_FORMATS = frozenset({"json", "jsonl", "csv", "text"})
_DEFAULT_MAPPING = {
    "content": "content",
    "source_record_id": "source_record_id",
    "source": "source",
    "occurred_at": "occurred_at",
    "metadata": "metadata",
}
_ALIASES = {
    "content": ("body", "text", "message", "letter", "line"),
    "source_record_id": ("id", "record_id", "letter_id", "source_id"),
    "source": ("source_name", "origin", "file"),
    "occurred_at": ("created_at", "date", "timestamp", "createdAt"),
    "metadata": ("provenance", "meta"),
}


@dataclass(frozen=True)
class ImportOptions:
    format: str | None = None
    encoding: str | None = None
    mapping: Mapping[str, str] = field(default_factory=dict)
    allowed_root: Path | None = None
    source_label: str | None = None
    dry_run: bool = False
    atomic: bool = True
    checkpoint_path: Path | None = None
    resume: bool = False


class ImportValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ImportReport:
    status: str
    source_label: str
    detected_format: str | None
    encoding: str | None
    seen: int = 0
    accepted: int = 0
    inserted: int = 0
    would_insert: int = 0
    duplicates: int = 0
    rejected: int = 0
    rolled_back: bool = False
    dry_run: bool = False
    resumed_from_line: int = 0
    last_line: int = 0
    errors: tuple[Mapping[str, Any], ...] = ()
    checkpoint: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_label": self.source_label,
            "detected_format": self.detected_format,
            "encoding": self.encoding,
            "seen": self.seen,
            "accepted": self.accepted,
            "inserted": self.inserted,
            "would_insert": self.would_insert,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "rolled_back": self.rolled_back,
            "dry_run": self.dry_run,
            "resumed_from_line": self.resumed_from_line,
            "last_line": self.last_line,
            "errors": [dict(item) for item in self.errors],
            "checkpoint": dict(self.checkpoint) if self.checkpoint else None,
        }


class LegacyLetterImporter:
    """Parse and insert only into the legacy read-only domain."""

    def __init__(self, memory: MemoryPort) -> None:
        self.memory = memory

    def import_file(
        self,
        path: str | os.PathLike[str],
        *,
        options: ImportOptions | None = None,
    ) -> ImportReport:
        options = options or ImportOptions()
        source_label = _source_label(options.source_label or Path(path).name)
        resolved, path_error = _resolve_path(path, options.allowed_root)
        if path_error is not None or resolved is None:
            return ImportReport(
                status="rejected",
                source_label=source_label,
                detected_format=None,
                encoding=None,
                rejected=1,
                errors=(path_error or {"line": 0, "code": "SOURCE_UNREADABLE"},),
            )
        try:
            raw = resolved.read_bytes()
        except (OSError, ValueError):
            return ImportReport(
                status="rejected",
                source_label=source_label,
                detected_format=None,
                encoding=None,
                rejected=1,
                errors=({"line": 0, "code": "SOURCE_UNREADABLE"},),
            )
        source_digest = hashlib.sha256(raw).hexdigest()
        detected_format = _detect_format(resolved, options.format)
        if detected_format is None:
            return ImportReport(
                status="rejected",
                source_label=source_label,
                detected_format=None,
                encoding=None,
                rejected=1,
                errors=({"line": 0, "code": "UNSUPPORTED_FORMAT"},),
            )
        try:
            text, encoding = _decode_bytes(raw, options.encoding)
        except ImportValidationError as exc:
            return ImportReport(
                status="rejected",
                source_label=source_label,
                detected_format=detected_format,
                encoding=None,
                rejected=1,
                errors=({"line": 0, "code": exc.code},),
            )
        except UnicodeError:
            return ImportReport(
                status="rejected",
                source_label=source_label,
                detected_format=detected_format,
                encoding=None,
                rejected=1,
                errors=({"line": 0, "code": "UNSUPPORTED_ENCODING"},),
            )

        start_line = self._resume_line(source_digest, options) if options.resume else 0
        raw_records, parse_errors = _parse_records(
            text,
            detected_format,
            start_line=start_line,
        )
        mapping = {**_DEFAULT_MAPPING, **dict(options.mapping)}
        records: list[LegacyLetter] = []
        errors: list[Mapping[str, Any]] = list(parse_errors)
        for line, raw_record in raw_records:
            try:
                records.append(_map_record(raw_record, mapping, source_label, line))
            except ValueError as exc:
                errors.append({"line": line, "code": str(exc)[:64]})

        observed_lines = [line for line, _ in raw_records]
        observed_lines.extend(
            int(error["line"])
            for error in errors
            if str(error.get("line", "")).isdigit()
        )
        last_line = max([start_line, *observed_lines], default=start_line)
        seen = len(raw_records)
        rejected = len(errors)
        try:
            existing = self.memory.legacy_content_hashes()
        except MemoryUnavailable:
            if options.dry_run:
                existing = set()
                errors.append({"line": 0, "code": "MEMORY_UNAVAILABLE"})
            else:
                return self._finish(
                    ImportReport(
                        status="unavailable",
                        source_label=source_label,
                        detected_format=detected_format,
                        encoding=encoding,
                        seen=seen,
                        accepted=len(records),
                        rejected=rejected + 1,
                        rolled_back=True,
                        resumed_from_line=start_line,
                        last_line=last_line,
                        errors=tuple([*errors, {"line": 0, "code": "MEMORY_UNAVAILABLE"}][:100]),
                    ),
                    raw,
                    options,
                    source_digest,
                )

        existing = set(existing)
        batch: set[str] = set()
        candidates: list[LegacyLetter] = []
        duplicates = 0
        for record in records:
            digest = hashlib.sha256(record.content.encode("utf-8")).hexdigest()
            if digest in existing or digest in batch:
                duplicates += 1
                batch.add(digest)
                continue
            batch.add(digest)
            candidates.append(record)

        if errors and options.atomic and not options.dry_run:
            return self._finish(
                ImportReport(
                    status="rolled_back",
                    source_label=source_label,
                    detected_format=detected_format,
                    encoding=encoding,
                    seen=seen,
                    accepted=len(records),
                    duplicates=duplicates,
                    rejected=rejected,
                    rolled_back=True,
                    resumed_from_line=start_line,
                    last_line=last_line,
                    errors=tuple(errors[:100]),
                ),
                raw,
                options,
                source_digest,
            )
        if options.dry_run:
            return self._finish(
                ImportReport(
                    status="dry_run",
                    source_label=source_label,
                    detected_format=detected_format,
                    encoding=encoding,
                    seen=seen,
                    accepted=len(records),
                    would_insert=len(candidates),
                    duplicates=duplicates,
                    rejected=rejected,
                    dry_run=True,
                    resumed_from_line=start_line,
                    last_line=last_line,
                    errors=tuple(errors[:100]),
                ),
                raw,
                options,
                source_digest,
            )

        try:
            result = self.memory.import_legacy_records(candidates, atomic=options.atomic)
        except MemoryUnavailable:
            result = None
        if result is None:
            report = ImportReport(
                status="unavailable",
                source_label=source_label,
                detected_format=detected_format,
                encoding=encoding,
                seen=seen,
                accepted=len(records),
                duplicates=duplicates,
                rejected=rejected + 1,
                rolled_back=True,
                resumed_from_line=start_line,
                last_line=last_line,
                errors=tuple([*errors, {"line": 0, "code": "MEMORY_UNAVAILABLE"}][:100]),
            )
        else:
            report = ImportReport(
                status="rolled_back" if result.rolled_back else "committed",
                source_label=source_label,
                detected_format=detected_format,
                encoding=encoding,
                seen=seen,
                accepted=len(records),
                inserted=result.inserted,
                duplicates=duplicates + result.duplicates,
                rejected=rejected + result.rejected,
                rolled_back=result.rolled_back,
                resumed_from_line=start_line,
                last_line=last_line,
                errors=tuple(errors[:100]),
            )
        return self._finish(report, raw, options, source_digest)

    def _resume_line(self, digest: str, options: ImportOptions) -> int:
        if options.checkpoint_path is None:
            return 0
        try:
            payload = json.loads(options.checkpoint_path.read_text(encoding="utf-8"))
            if payload.get("source_sha256") == digest and payload.get("status") in {"committed", "dry_run"}:
                return max(0, int(payload.get("next_line", 0)))
        except (OSError, UnicodeError, ValueError, TypeError):
            pass
        return 0

    def _finish(
        self,
        report: ImportReport,
        raw: bytes,
        options: ImportOptions,
        source_digest: str,
    ) -> ImportReport:
        checkpoint_path = options.checkpoint_path
        if checkpoint_path is None:
            return report
        if report.status not in {"committed", "dry_run"}:
            return report
        payload = {
            "schema_version": 1,
            "status": report.status,
            "source_sha256": source_digest,
            "next_line": report.last_line,
            "processed_records": report.seen,
        }
        try:
            checkpoint_path = Path(checkpoint_path)
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix="memory-checkpoint-",
                suffix=".tmp",
                dir=checkpoint_path.parent,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                os.replace(temporary, checkpoint_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            payload = {"schema_version": 1, "status": "checkpoint_unavailable"}
        return ImportReport(**{**report.__dict__, "checkpoint": payload})


def _source_label(value: str) -> str:
    text = str(value or "legacy-import").replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1][:256] or "legacy-import"


def _resolve_path(
    path: str | os.PathLike[str],
    allowed_root: Path | None,
) -> tuple[Path | None, Mapping[str, Any] | None]:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None, {"line": 0, "code": "SOURCE_UNREADABLE"}
    if not resolved.is_file():
        return None, {"line": 0, "code": "SOURCE_NOT_FILE"}
    if allowed_root is not None:
        try:
            root = Path(allowed_root).expanduser().resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None, {"line": 0, "code": "PATH_ESCAPE"}
    return resolved, None


def _detect_format(path: Path, requested: str | None) -> str | None:
    if requested:
        normalized = requested.casefold().lstrip(".")
        if normalized == "ndjson":
            normalized = "jsonl"
        return normalized if normalized in SUPPORTED_FORMATS else None
    suffix = path.suffix.casefold().lstrip(".")
    if suffix == "ndjson":
        suffix = "jsonl"
    return suffix if suffix in SUPPORTED_FORMATS else None


def _decode_bytes(raw: bytes, requested: str | None) -> tuple[str, str]:
    if requested:
        try:
            return raw.decode(requested), requested
        except LookupError:
            raise ImportValidationError("INVALID_ENCODING") from None
        except UnicodeDecodeError:
            raise ImportValidationError("INVALID_ENCODING") from None
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return raw.decode("utf-32"), "utf-32"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    for encoding in ("utf-8", "cp932", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeError("source encoding is not supported")


def _parse_records(
    text: str,
    detected_format: str,
    *,
    start_line: int = 0,
) -> tuple[list[tuple[int, Any]], list[Mapping[str, Any]]]:
    errors: list[Mapping[str, Any]] = []
    if detected_format == "text":
        return [
            (line_number, {"line": line, "_line": line_number})
            for line_number, line in enumerate(text.splitlines(), start=1)
            if line_number > start_line and line.strip()
        ], errors
    if detected_format == "jsonl":
        records: list[tuple[int, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line_number <= start_line or not line.strip():
                continue
            try:
                records.append((line_number, json.loads(line)))
            except (TypeError, ValueError, UnicodeError):
                errors.append({"line": line_number, "code": "BAD_JSON_LINE"})
        return records, errors
    if detected_format == "csv":
        records = []
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return [], [{"line": 1, "code": "CSV_HEADER_MISSING"}]
        for line_number, row in enumerate(reader, start=2):
            if line_number <= start_line:
                continue
            if None in row:
                errors.append({"line": line_number, "code": "CSV_BAD_COLUMNS"})
                continue
            records.append((line_number, row))
        return records, errors
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError, UnicodeError):
        return [], [{"line": 1, "code": "BAD_JSON"}]
    if isinstance(loaded, list):
        values = loaded
    elif isinstance(loaded, Mapping):
        values = next(
            (
                loaded[key]
                for key in ("letters", "records", "data")
                if isinstance(loaded.get(key), list)
            ),
            [loaded],
        )
    else:
        values = [loaded]
    return [
        (index, value)
        for index, value in enumerate(values, start=1)
        if index > start_line
    ], errors


def _lookup(record: Any, field: str, *, line: int) -> Any:
    if field in {"line", "text"} and isinstance(record, Mapping) and "line" in record:
        return record.get("line")
    current = record
    for part in str(field).split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            raise ValueError(f"missing_{field[:40]}")
    return current


def _mapped_value(record: Any, canonical: str, mapping: Mapping[str, str], *, line: int) -> Any:
    field = mapping.get(canonical)
    candidates = [field] if field else []
    candidates.extend(_ALIASES.get(canonical, (canonical,)))
    if isinstance(record, Mapping):
        for candidate in candidates:
            if candidate and candidate in record:
                return record[candidate]
    if field:
        return _lookup(record, field, line=line)
    raise ValueError(f"missing_{canonical}")


def _map_record(
    record: Any,
    mapping: Mapping[str, str],
    source_label: str,
    line: int,
) -> LegacyLetter:
    if not isinstance(record, Mapping):
        raise ValueError("record_not_object")
    content = _mapped_value(record, "content", mapping, line=line)
    if content is None or not str(content).strip():
        raise ValueError("content_required")
    try:
        source_record_id = _mapped_value(record, "source_record_id", mapping, line=line)
    except ValueError:
        source_record_id = f"generated-{hashlib.sha256(str(content).encode('utf-8')).hexdigest()[:16]}"
    try:
        source = _mapped_value(record, "source", mapping, line=line)
    except ValueError:
        source = source_label
    try:
        occurred_at = _mapped_value(record, "occurred_at", mapping, line=line)
    except ValueError:
        occurred_at = None
    metadata: dict[str, Any] = {"input_line": line}
    try:
        supplied = _mapped_value(record, "metadata", mapping, line=line)
    except ValueError:
        supplied = None
    if supplied is not None:
        if isinstance(supplied, str):
            try:
                supplied = json.loads(supplied)
            except (TypeError, ValueError, UnicodeError):
                raise ValueError("metadata_invalid_json") from None
        if not isinstance(supplied, Mapping):
            raise ValueError("metadata_not_object")
        metadata.update(dict(supplied))
    return LegacyLetter(
        content=str(content),
        source_record_id=str(source_record_id),
        source=_source_label(str(source)),
        occurred_at=occurred_at,
        metadata=metadata,
    )


__all__ = ["ImportOptions", "ImportReport", "LegacyLetterImporter"]
