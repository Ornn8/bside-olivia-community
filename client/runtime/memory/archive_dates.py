"""Approximate dates for imported letters that carry none, from what the import still knows.

Two sources, in order: the reply time the backup kept for that exchange, then the nearest
dated letters before and after it in the same file. A letter with a dated neighbour on only
one side stays unknown: exports place undated letters at the end of the file, so a one-sided
neighbour says nothing about when they were written.

An estimate is derived on every read. The Archive row, export and re-import keep
``occurred_at`` unknown, and prompts label the value as an estimate, never an original date.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .recall_sources import _original_time

PAIR_KINDS = frozenset({"official_text_reply", "offline_recovered_text_reply", "local_letter_backup_v1"})
_BEIJING = timezone(timedelta(hours=8))
# Rows of one import are inserted in one transaction; a later import of another backup
# starts its own file order even when the clock barely moved.
_IMPORT_GAP_SECONDS = 600
_BASIS_TEXT = {"reply_time": "按这次回信的时间", "neighbouring_letters": "按前后的信推算"}


def _day(value: datetime) -> str:
    local = value.astimezone(_BEIJING)
    return f"{local.year}年{local.month}月{local.day}日"


@dataclass(frozen=True)
class DateEstimate:
    stamp: datetime  # a sortable point inside the bounds
    earliest: datetime
    latest: datetime
    basis: str  # "reply_time" or "neighbouring_letters"

    def label(self) -> str:
        """Plain Chinese wording of the range, by Beijing date."""
        if _day(self.earliest) == _day(self.latest):
            return "大约" + _day(self.earliest)
        return f"大约在{_day(self.earliest)}到{_day(self.latest)}之间"

    def to_dict(self) -> dict:
        return {"estimated_time": self.stamp.isoformat(), "estimate": self.label(),
                "time_basis": "estimated_from_" + self.basis}


def known_time(row: Mapping) -> datetime | None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    if metadata.get("timestamp_known") is False:
        return None
    return _original_time(row.get("occurred_at"))


def _reply_time(row: Mapping) -> datetime | None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    record = metadata.get("backup_record") if isinstance(metadata.get("backup_record"), Mapping) else {}
    return _original_time(metadata.get("replied_at")) or _original_time(record.get("replied_at"))


def _file_order(row: Mapping) -> tuple[str, int] | None:
    """(import group, position) for rows whose import kept the file order."""
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    source = row.get("source_record_id")
    if not isinstance(source, str) or metadata.get("import_kind") not in PAIR_KINDS:
        return None
    if source.startswith("offline-letter-pairs:"):
        prefix, _, index = source.rpartition(":")
        return (prefix, int(index)) if index.isdigit() else None
    position = metadata.get("import_position")
    if type(position) is not int or position < 0:
        return None
    return ("import:" + str(row.get("source", "")), position)


def _import_groups(rows: Iterable[Mapping]) -> list[list[tuple[int, Mapping]]]:
    groups: dict[str, list[tuple[int, int, Mapping]]] = {}
    for row in rows:
        order = _file_order(row)
        if order is None:
            continue
        imported = row.get("imported_at")
        groups.setdefault(order[0], []).append((imported if type(imported) is int else 0, order[1], row))
    result = []
    for members in groups.values():
        members.sort(key=lambda item: (item[0], item[1]))
        current, last = [], None
        for imported, position, row in members:
            # A new backup restarts its file order; never mix two files' positions.
            if last is not None and (position <= last[1] or imported - last[0] > _IMPORT_GAP_SECONDS):
                result.append(current)
                current = []
            current.append((position, row))
            last = (imported, position)
        if current:
            result.append(current)
    return result


def estimate_dates(rows: Iterable[Mapping]) -> dict[str, DateEstimate]:
    """source_record_id -> estimate, for undated rows that the import can still place."""
    rows = [row for row in rows if isinstance(row, Mapping)]
    estimates: dict[str, DateEstimate] = {}
    for row in rows:
        source, replied = row.get("source_record_id"), _reply_time(row)
        if isinstance(source, str) and known_time(row) is None and replied is not None:
            estimates[source] = DateEstimate(replied, replied, replied, "reply_time")
    for group in _import_groups(rows):
        anchors = []
        for position, row in group:
            stamp = known_time(row) or (estimates[row["source_record_id"]].stamp
                                        if row["source_record_id"] in estimates else None)
            if stamp is not None:
                anchors.append((position, stamp))
        if len(anchors) < 2 or len(anchors) == len(group):
            continue
        for position, row in group:
            source = row["source_record_id"]
            if known_time(row) is not None or source in estimates:
                continue
            before = next((item for item in reversed(anchors) if item[0] < position), None)
            after = next((item for item in anchors if item[0] > position), None)
            if before is None or after is None:
                continue  # one-sided: undated letters may simply have been sorted to the end
            low, high = sorted((before[1], after[1]))
            # Interpolate by file position from whichever neighbour is older.
            older, newer = (before, after) if before[1] <= after[1] else (after, before)
            share = abs(position - older[0]) / abs(newer[0] - older[0])
            estimates[source] = DateEstimate(low + (high - low) * share, low, high, "neighbouring_letters")
    return estimates


def ordering_time(row: Mapping, estimates: Mapping[str, DateEstimate]) -> datetime | None:
    """Original time, else the estimate; None when neither exists."""
    stamp = known_time(row)
    if stamp is not None:
        return stamp
    estimate = estimates.get(row.get("source_record_id"))
    return estimate.stamp if estimate else None


def when_text(stamp: datetime | None, estimate: DateEstimate | None = None, *, quoted_before: datetime | None = None) -> str:
    if stamp is not None:
        return _day(stamp)
    if estimate is not None:
        return estimate.label() + "（原信没写日期，" + _BASIS_TEXT[estimate.basis] + "）"
    if quoted_before is not None:
        return "早于" + _day(quoted_before) + "（原信没写日期，那天的信里已经抄过它）"
    return "很久以前（原信没写日期）"


__all__ = ["DateEstimate", "estimate_dates", "known_time", "ordering_time", "when_text", "PAIR_KINDS"]
