"""Recalled originals carry how long ago they happened, computed by the app, so a letter from weeks ago is never
told as 'yesterday' (user report 2026-10-21: the 9-29 letter recalled as 昨天)."""
from datetime import datetime, timedelta, timezone

from runtime.memory.memory_prompt import relative_day

NOW = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)  # 09:00 in Beijing


def test_relative_day_by_beijing_date():
    assert relative_day('2026-10-22T00:30:00+00:00', NOW) == '今天'
    assert relative_day('2026-10-21T15:59:00+00:00', NOW) == '昨天'        # 23:59 Beijing on 10-21
    assert relative_day('2026-10-21T16:01:00+00:00', NOW) == '今天'        # 00:01 Beijing on 10-22
    assert relative_day('2026-10-20T12:00:00+08:00', NOW) == '前天'
    assert relative_day('2026-09-29T16:30:00+08:00', NOW) == '23天前'
    assert relative_day('2026-09-29T08:30:00Z', NOW) == '23天前'


def test_unknown_or_future_times_get_no_label():
    for value in (None, '', 'not a time', '2026-09-29T16:30:00', '2026-10-25T10:00:00+08:00'):
        assert relative_day(value, NOW) is None
