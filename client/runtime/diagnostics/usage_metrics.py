"""Content-free daily provider usage totals; missing usage is never zero usage."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from collections.abc import Mapping


def normalize_usage(value: object) -> dict[str, int | None]:
    try:
        if callable(getattr(value, 'model_dump', None)):
            value = value.model_dump()
    except Exception:
        # SDK diagnostics are optional; never replace a successful model result.
        value = None
    source = value if isinstance(value, Mapping) else {}
    def number(*keys):
        for key in keys:
            found = source
            for part in key.split('.'):
                found = found.get(part) if isinstance(found, Mapping) else None
            if type(found) is int and 0 <= found <= 10**12:
                return found
        return None
    result = {
        'input_tokens': number('prompt_tokens', 'input_tokens'),
        'output_tokens': number('completion_tokens', 'output_tokens'),
        'cached_tokens': number('prompt_cache_hit_tokens', 'prompt_tokens_details.cached_tokens', 'input_tokens_details.cached_tokens'),
        'reasoning_tokens': number('completion_tokens_details.reasoning_tokens', 'output_tokens_details.reasoning_tokens'),
        'uncached_tokens': number('prompt_cache_miss_tokens'),
    }
    if result['uncached_tokens'] is None and result['input_tokens'] is not None and result['cached_tokens'] is not None:
        result['uncached_tokens'] = max(0, result['input_tokens'] - result['cached_tokens'])
    return result


def purpose_for(request_id: str) -> str:
    # Never persist arbitrary request IDs, which may contain private text.
    for fragment, purpose in (
        ('proactive', 'proactive'), ('router', 'routing'), ('triage', 'routing'),
        ('day:', 'world_refresh'), ('life:', 'world_extract'),
        ('voice-direction', 'voice_direction'), ('song', 'song'),
        ('letter-reply:', 'reply'),
    ):
        if fragment in request_id:
            return purpose
    return 'other'


def record_usage(usage: object, *, purpose: str, outcome: str) -> None:
    """Aggregate each network attempt, including billed invalid/truncated output."""
    configured = os.environ.get('OLIVIA_LOCAL_DATA_ROOT')
    if not configured or not Path(configured).is_absolute():
        return
    purpose = purpose if purpose in {'proactive', 'routing', 'world_refresh', 'world_extract', 'voice_direction', 'song', 'reply', 'memory', 'other'} else 'other'
    outcome = outcome if outcome in {'response', 'error', 'timeout', 'transport_error'} else 'error'
    counters = normalize_usage(usage)
    day = datetime.now(timezone.utc).date().isoformat()
    try:
        folder = Path(configured) / 'diagnostics'
        folder.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(folder / 'token-usage.sqlite3', timeout=0.1) as db:
            db.execute('CREATE TABLE IF NOT EXISTS usage (day TEXT, purpose TEXT, outcome TEXT, metric TEXT, total INTEGER, samples INTEGER, PRIMARY KEY(day,purpose,outcome,metric))')
            for metric, amount in {'attempts': 1, **counters}.items():
                db.execute('INSERT INTO usage VALUES (?,?,?,?,?,?) ON CONFLICT(day,purpose,outcome,metric) DO UPDATE SET total=total+excluded.total,samples=samples+excluded.samples',
                           (day, purpose, outcome, metric, amount or 0, int(amount is not None)))
            db.execute("DELETE FROM usage WHERE day < date('now','-90 days')")
    except (OSError, sqlite3.Error):
        # Accounting must not interrupt a reply or a memory transaction.
        logging.getLogger(__name__).warning('TOKEN_USAGE_WRITE_FAILED')


if __name__ == '__main__':
    root = Path(os.environ['OLIVIA_LOCAL_DATA_ROOT']) / 'diagnostics/token-usage.sqlite3'
    with sqlite3.connect(f'{root.as_uri()}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        print(json.dumps([dict(row) for row in db.execute('SELECT * FROM usage ORDER BY day,purpose,outcome,metric')], indent=2))
