"""Read official Olivia text replies into the local read-only archive."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


_HEADER_NAMES = (
    "x-token",
    "x-device_id",
    "x-device_model",
    "x-language",
    "x-lifecycle_id",
    "x-pkg_version",
    "x-sys_version",
    "x-uid",
    "x-platform",
    "x-bundle_id",
    "x-client_type",
)
OFFICIAL_API_BASE = "https://toy-cnbeta01.olivia.miyoushe.com/toy"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def build_legacy_import_payload(
    letters: Iterable[Mapping[str, Any]],
    *,
    account_id: str,
) -> dict[str, object]:
    """Map official details to the existing local legacy-import contract."""

    account = _text(account_id)
    if not account:
        raise ValueError("official account id is required")
    records: list[dict[str, object]] = []
    for letter in letters:
        letter_id = _text(letter.get("letter_id"))
        user_content = _text(letter.get("content"))
        reply_text = _text(letter.get("reply_text"))
        if not letter_id or not user_content or not reply_text:
            continue
        records.append(
            {
                "source_record_id": f"official:{account}:{letter_id}",
                "source": "official-olivia",
                "occurred_at": letter.get("created_at"),
                "content": f"用户来信：{user_content}\n林离回信：{reply_text}",
                "metadata": {
                    "user_content": user_content,
                    "reply_text": reply_text,
                    "replied_at": letter.get("replied_at"),
                    "import_kind": "official_text_reply",
                },
            }
        )
    return {"mode": "read_only", "letters": records}


def _headers_from_log(log_path: str | Path) -> dict[str, str]:
    text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        if '"x-token"' not in line or (
            "network_request" not in line
            and '"request.url":"/signIn"' not in line
        ):
            continue
        headers: dict[str, str] = {}
        for name in _HEADER_NAMES:
            match = re.search(rf'"{re.escape(name)}":"([^"\\]*)"', line)
            if match:
                headers[name] = match.group(1)
        if headers.get("x-token") and headers.get("x-uid"):
            headers["User-Agent"] = "Olivia/" + headers.get(
                "x-pkg_version",
                "0.0.9.627",
            )
            return headers
    raise ValueError("OFFICIAL_LOGIN_REQUIRED")


def collect_official_text_replies(
    log_path: str | Path,
    *,
    request_json,
) -> dict[str, object]:
    """Fetch one official mailbox snapshot without retaining credentials."""

    headers = _headers_from_log(log_path)
    items: list[object] = []
    cursor: object = 0
    for _page in range(100):
        listing = request_json(
            "/letter/list?cursor=" + quote(str(cursor), safe="") + "&page_size=50",
            headers,
        )
        if not isinstance(listing, Mapping) or listing.get("code") != 0:
            raise ValueError("OFFICIAL_LETTER_LIST_UNAVAILABLE")
        data = listing.get("data")
        page_items = data.get("list") if isinstance(data, Mapping) else None
        if not isinstance(page_items, list):
            raise ValueError("OFFICIAL_LETTER_LIST_INVALID")
        items.extend(page_items)
        has_more = data.get("has_more", data.get("hasMore", False))
        if has_more is not True:
            break
        next_cursor = data.get("next_cursor", data.get("nextCursor"))
        if next_cursor in (None, "") or next_cursor == cursor:
            raise ValueError("OFFICIAL_LETTER_LIST_INVALID")
        cursor = next_cursor
    else:
        raise ValueError("OFFICIAL_LETTER_LIST_INVALID")
    details: list[Mapping[str, Any]] = []
    for item in items:
        letter_id = _text(item.get("letter_id")) if isinstance(item, Mapping) else ""
        if not letter_id:
            continue
        detail = request_json(
            "/letter/detail?letter_id=" + quote(letter_id, safe=""),
            headers,
        )
        if not isinstance(detail, Mapping) or detail.get("code") != 0:
            continue
        value = detail.get("data")
        if isinstance(value, Mapping):
            details.append({"letter_id": letter_id, **dict(value)})
    return build_legacy_import_payload(details, account_id=headers["x-uid"])


def _request_official_json(path: str, headers: dict[str, str]) -> dict[str, Any]:
    request = Request(OFFICIAL_API_BASE + path, headers=headers)
    with urlopen(request, timeout=30) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("OFFICIAL_RESPONSE_INVALID")
    return value


def default_official_log_path(
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    appdata = _text(values.get("APPDATA"))
    if not appdata:
        raise ValueError("OFFICIAL_LOG_UNAVAILABLE")
    return Path(appdata) / "miHoYo" / "Olivia-steam" / "logs" / "Olivia.log"


def collect_default_official_text_replies() -> dict[str, object]:
    return collect_official_text_replies(
        default_official_log_path(),
        request_json=_request_official_json,
    )
