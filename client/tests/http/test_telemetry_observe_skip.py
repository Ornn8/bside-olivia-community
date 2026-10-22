"""A store save looks again only at rows whose telemetry fields changed (2026-10-22: every save read and rewrote one
record per recent row, seconds per reply for heavy accounts)."""
import sqlite3

from runtime.diagnostics.reply_telemetry import Collector


def written(tmp_path):
    with sqlite3.connect(tmp_path / 'reply-telemetry.sqlite3') as db:
        return dict(db.execute('SELECT trace, at FROM observed'))


def test_unchanged_rows_are_skipped_and_every_change_is_still_seen(tmp_path):
    clock = [1000]
    c = Collector(tmp_path, version='2.4.18', clock=lambda: clock[0])
    c.account('synthetic-key'); c.set_enabled(True)
    rows = [dict(letter_id=f'qq:{i}', channel='qq', created_at=1001, delivery_status='DELIVERED',
                 reply_timing_seconds={'writer': 1.0}) for i in range(3)]
    clock[0] = 1002; c.observe(rows)
    first = written(tmp_path)
    clock[0] = 1003; c.observe(rows)
    assert written(tmp_path) == first                            # nothing changed: nothing rewritten
    rows[1]['image_status'] = 'COMPLETED'                        # a field changed in place
    rows[2]['reply_timing_seconds']['writer'] = 2.0              # so did a nested value
    clock[0] = 1004; c.observe(rows)
    after = written(tmp_path)
    assert [after[t] == first[t] for t in sorted(first)].count(False) == 2
    media = [e for e in c.batch() if e['stage'] == 'media']
    assert [e['status'] for e in media] == ['ok']
    c.set_enabled(False); c.set_enabled(True)                    # a fresh collection sees every row again
    for row in rows:
        row['created_at'] = 1006                                 # rows after the new start
    clock[0] = 1007; c.observe(rows)
    assert len(written(tmp_path)) == 3
