"""Local, read-only paging options shared by the memory and original browsers."""

from datetime import datetime


def validate_options(*, query=None, page=1, limit=20, days=0, sort="new"):
    if (query is not None and (not isinstance(query, str) or len(query) > 500)
            or type(page) is not int or not 1 <= page <= 1_000_000
            or type(limit) is not int or not 1 <= limit <= 100
            or type(days) is not int or days not in (0, 7, 30, 90)
            or sort not in ("new", "old")):
        raise ValueError("MEMORY_BROWSE_OPTIONS_INVALID")


def page_bounds(total, page, limit):
    page = min(page, max(1, (total + limit - 1) // limit))
    return page, (page - 1) * limit


def record_time(record):
    value = record.metadata.get("updated_at")
    try:
        updated = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        updated = None
    return (updated if updated and updated.utcoffset() is not None else None) or record.occurred_at or record.created_at
